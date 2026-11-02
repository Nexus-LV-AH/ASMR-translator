#!/usr/bin/env python3
"""日语音声字幕提取入口（第一阶段：只提取日文字幕 txt）。

用法：
    python scripts/extract_ja.py input/                 # 处理整个目录
    python scripts/extract_ja.py input/xxx.mp3           # 处理单个文件
    python scripts/extract_ja.py input/ --force          # 强制重跑
    python scripts/extract_ja.py input/ --model whisper-ja-1.5b --device cpu

流程：解码（PyAV）→ Silero VAD 分段 → anime-whisper 批量转写 → 组装字幕 → 写 txt
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import audio_io  # noqa: E402
import segmenter  # noqa: E402
import subtitle_builder as sb  # noqa: E402

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


# ---------------------------------------------------------------- 环境与配置
def setup_environment() -> None:
    """HF 直连在本机不可用，默认走镜像；缓存全部落在项目内。"""
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HOME", str(PROJECT_ROOT / ".models"))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def load_config(path: Path) -> dict:
    """读配置；同目录有 config.local.yaml 时递归覆盖（与其它脚本一致）。"""
    if not path.exists():
        logging.warning("未找到配置文件 %s，使用内置默认值", path)
        return {}
    try:
        import translate as T

        return T.load_config(path)
    except Exception:  # noqa: BLE001
        import yaml

        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}


def resolve_model_id(cfg: dict, model_arg: str | None) -> tuple[str, str]:
    """返回 (模型 key, 模型路径或 HF id)。

    优先使用本地目录（scripts/fetch_model.py 下载的），缺失时自动下载后再用；
    --model 也可直接给 HF 模型 id 或本地目录路径。
    """
    registry = (cfg.get("model") or {}).get("registry") or {}
    default_key = (cfg.get("model") or {}).get("default") or "anime-whisper"

    key = model_arg or default_key

    if key in registry:
        entry = registry[key]
        local = entry.get("local_dir")
        if local:
            local_path = Path(local) if Path(local).is_absolute() else PROJECT_ROOT / local
            if not (local_path / "model.safetensors").exists():
                logging.info("本地未找到模型 %s，开始下载（可中断，支持续传）...", local_path)
                from fetch_model import fetch

                fetch(entry["hf_id"], local_path)
            return key, str(local_path)
        return key, entry["hf_id"]

    candidate = Path(key)
    if candidate.exists():
        return key, str(candidate)
    if "/" in key:  # 直接传 HF id（由 transformers 自行下载）
        return key, key

    raise SystemExit(
        f"未知模型 {key!r}。可选：{', '.join(registry)}，或直接传 HuggingFace 模型 id（需含 '/'）"
    )


# ---------------------------------------------------------------- 处理流程
def collect_media(paths: list[str]) -> list[Path]:
    """收集音频：目录会递归扫描（支持 input/作品名/xxx.mp3 这种分层）。"""
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files.extend(sorted(f for f in p.rglob("*") if f.is_file() and audio_io.is_media_file(f)))
        elif p.is_file() and audio_io.is_media_file(p):
            files.append(p)
        elif p.is_file():
            logging.warning("跳过不支持的文件类型：%s", p)
        else:
            logging.warning("路径不存在：%s", p)
    # 去重并保持顺序
    seen: set[Path] = set()
    unique: list[Path] = []
    for f in files:
        rp = f.resolve()
        if rp not in seen:
            seen.add(rp)
            unique.append(f)
    return unique


def resolve_output_dir(media: Path, output_root: Path, input_root: Path) -> Path:
    """镜像 input 的子目录结构：input/作品A/x.mp3 → output/作品A/。

    音频不在 input_root 之下（用户显式传了别的路径）时，落 output_root 根目录；
    若会与已有同名产物冲突，则加 -1/-2 后缀区分。
    """
    media_abs = media.resolve()
    try:
        rel = media_abs.relative_to(input_root.resolve())
        target = output_root / rel.parent
    except ValueError:
        target = output_root
    return target


def process_file(
    media: Path,
    *,
    engine_factory,
    cfg: dict,
    args: argparse.Namespace,
    output_dir: Path,
) -> dict:
    stem = media.stem
    output_dir.mkdir(parents=True, exist_ok=True)
    txt_path = output_dir / f"{stem}.ja.txt"

    if txt_path.exists() and not args.force:
        logging.info("跳过（已存在 %s，用 --force 可重跑）", txt_path.relative_to(PROJECT_ROOT))
        return {"file": str(media), "status": "skipped", "output_dir": str(output_dir)}

    logging.info("=" * 68)
    logging.info("开始处理：%s", media.name)
    t_start = time.time()

    logging.info("[1/4] 解码音频（PyAV，无需 ffmpeg）...")
    audio = audio_io.decode_audio(media)
    duration = audio_io.duration_seconds(audio)
    logging.info("     时长 %.1f 秒（%.1f 分钟），采样点 %d", duration, duration / 60, len(audio))

    logging.info("[2/4] 切分窗口...")
    t_vad = time.time()
    segments, seg_info = segmenter.build_segments(audio, cfg.get("segment") or {})
    if not segments:
        logging.error("     分段结果为空，跳过该文件")
        return {"file": media.name, "status": "failed", "error": "no segments"}
    logging.info(
        "     模式 %s：%d 个窗口，跳过近静音 %d 个（用时 %.1fs）",
        seg_info.get("mode"),
        len(segments),
        seg_info.get("skipped_silent", 0),
        time.time() - t_vad,
    )

    chunks = [
        audio[int(seg.start_ms / 1000 * audio_io.SAMPLING_RATE) : int(seg.end_ms / 1000 * audio_io.SAMPLING_RATE)]
        for seg in segments
    ]

    logging.info("[3/4] anime-whisper 转写 %d 段...", len(chunks))
    t_asr = time.time()

    def progress(done: int, total: int) -> None:
        logging.info("     转写进度 %d/%d（%.0f%%）", done, total, done / max(1, total) * 100)

    engine = engine_factory(progress)
    texts = engine.transcribe_batch(chunks)
    asr_elapsed = time.time() - t_asr
    logging.info("     转写完成（用时 %.1fs，速度 %.2fx 实时）", asr_elapsed, duration / max(1e-6, asr_elapsed))

    logging.info("[4/4] 组装字幕...")
    out_cfg = cfg.get("output") or {}
    collapse = int((cfg.get("postprocess") or {}).get("collapse_repeats", 3))
    lines, stats = sb.build_lines(
        segments,
        texts,
        collapse_repeats=collapse,
        split_mode=str(out_cfg.get("split_lines", "punctuation")),
        split_min_chars=int(out_cfg.get("split_min_chars", 8)),
        split_max_chars=int(out_cfg.get("split_max_chars", 60)),
    )

    meta = {
        "file": media.name,
        "audio_duration_s": round(duration, 3),
        "model": {
            "hf_id": getattr(engine, "settings").hf_id,
            "device": str(engine.device),
            "dtype": str(getattr(engine, "dtype", "")),
            "generate_kwargs": getattr(engine, "generate_kwargs", {}),
            "note": "anime-whisper 官方要求：不得设置 initial prompt",
        },
        "segment": {**(cfg.get("segment") or {}), **seg_info},
        "stats": stats,
        "timing_s": {
            "total": round(time.time() - t_start, 2),
            "asr": round(asr_elapsed, 2),
        },
        "written_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    written = sb.write_outputs(
        stem=stem,
        lines=lines,
        output_dir=output_dir,
        stats=stats,
        meta=meta,
        write_timing=bool(out_cfg.get("write_timing_json", True)),
        write_meta=bool(out_cfg.get("write_meta_json", True)),
    )

    logging.info("完成：%d 条字幕 → %s（总用时 %.1fs）", len(lines), written["txt"].name, time.time() - t_start)
    return {
        "file": media.name,
        "status": "done",
        "lines": len(lines),
        "empty_segments": stats["empty_segments"],
        "collapsed": stats["collapsed_repeat_lines"],
        "duration_s": round(duration, 1),
        "elapsed_s": round(time.time() - t_start, 1),
        "txt": str(written["txt"]),
    }


# ---------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="日语音声字幕提取（anime-whisper，只输出日文字幕 txt）"
    )
    p.add_argument("inputs", nargs="*", default=None, help="音频文件或目录（默认读 config 的 input_dir）")
    p.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"), help="配置文件路径")
    p.add_argument("--model", default=None, help="模型注册名（anime-whisper/kotoba-whisper-v2.2/whisper-ja-1.5b）或 HF 模型 id")
    p.add_argument("--device", default=None, help="auto | mps | cpu")
    p.add_argument("--dtype", default=None, help="float16 | float32")
    p.add_argument("--batch-size", type=int, default=None)
    p.add_argument("--output-dir", default=None, help="字幕输出目录")
    p.add_argument("--segment", default=None, choices=["fixed", "vad"], help="分段策略（默认 fixed）")
    p.add_argument("--window-ms", type=int, default=None, help="窗口长度（毫秒，默认 12000）")
    p.add_argument("--no-snap", action="store_true", help="关闭窗口边界吸附静音谷")
    p.add_argument("--silence-gate-db", type=float, default=None, help="近静音窗口门限（相对噪声底 dB，0=关闭）")
    p.add_argument("--split-lines", default=None, choices=["punctuation", "none"], help="窗口内是否按标点细分")
    p.add_argument("--vad-threshold", type=float, default=None, help="Silero VAD 阈值（--segment vad 时生效）")
    p.add_argument("--merge-gap-ms", type=int, default=None, help="相邻语音段合并的最大间隙（毫秒）")
    p.add_argument("--max-seg-sec", type=float, default=None, help="等同 --window-ms（以秒给出窗口长度）")
    p.add_argument("--collapse-repeats", type=int, default=None, help="连续重复行折叠阈值（0 关闭）")
    p.add_argument("--no-timing", action="store_true", help="不写 timing.json")
    p.add_argument("--force", action="store_true", help="已存在字幕也重跑")
    p.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return p


def apply_overrides(cfg: dict, args: argparse.Namespace) -> dict:
    cfg = json.loads(json.dumps(cfg))  # 深拷贝，避免污染
    cfg.setdefault("paths", {})
    cfg.setdefault("model", {})
    cfg.setdefault("segment", {})
    cfg.setdefault("postprocess", {})
    cfg.setdefault("output", {})

    if args.device:
        cfg["model"]["device"] = args.device
    if args.dtype:
        cfg["model"]["dtype"] = args.dtype
    if args.batch_size:
        cfg["model"]["batch_size"] = args.batch_size
    if args.segment:
        cfg["segment"]["mode"] = args.segment
    if args.window_ms:
        cfg["segment"]["window_ms"] = args.window_ms
    if args.no_snap:
        cfg["segment"]["snap"] = False
    if args.silence_gate_db is not None:
        cfg["segment"]["silence_gate_db"] = args.silence_gate_db
    if args.split_lines:
        cfg["output"]["split_lines"] = args.split_lines
    if args.vad_threshold is not None:
        cfg["segment"].setdefault("vad", {})["threshold"] = args.vad_threshold
    if args.merge_gap_ms is not None:
        cfg["segment"].setdefault("vad", {})["merge_gap_ms"] = args.merge_gap_ms
    if args.max_seg_sec is not None:
        cfg["segment"]["window_ms"] = int(args.max_seg_sec * 1000)
    if args.collapse_repeats is not None:
        cfg["postprocess"]["collapse_repeats"] = args.collapse_repeats
    if args.no_timing:
        cfg["output"]["write_timing_json"] = False
    return cfg


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=LOG_FORMAT,
        stream=sys.stdout,
    )

    setup_environment()
    cfg = apply_overrides(load_config(Path(args.config)), args)

    model_key, hf_id = resolve_model_id(cfg, args.model)
    model_cfg = cfg.get("model") or {}
    gen_cfg = cfg.get("generation") or {}

    paths_cfg = cfg.get("paths") or {}
    # 日文中间产物独立目录（不再与中文产物混在 output/）
    output_root = Path(args.output_dir or paths_cfg.get("subtitles_dir") or "subtitles_ja")
    if not output_root.is_absolute():
        output_root = PROJECT_ROOT / output_root
    input_root = Path(paths_cfg.get("input_dir") or "input")
    if not input_root.is_absolute():
        input_root = PROJECT_ROOT / input_root

    inputs = args.inputs or [str(input_root)]
    media_files = collect_media(inputs)
    if not media_files:
        logging.error("没有找到可处理的音频文件。请把 mp3/wav/flac 放进 input/（可按作品建子文件夹）或用命令行指定路径。")
        return 2

    logging.info("待处理文件 %d 个：%s", len(media_files), ", ".join(f.name for f in media_files))
    logging.info("模型：%s (%s)", model_key, hf_id)
    logging.info("日文字幕目录：%s（按 input 的子目录结构镜像）", output_root)

    # 懒加载重型依赖：只有真正要处理文件时才导入
    from asr_engine import AnimeWhisperEngine, AsrSettings

    settings = AsrSettings(
        hf_id=hf_id,
        device=model_cfg.get("device", "auto"),
        dtype=model_cfg.get("dtype", "float16"),
        batch_size=int(model_cfg.get("batch_size", 32)),
        chunk_length_s=float(model_cfg.get("chunk_length_s", 30.0)),
        language=gen_cfg.get("language", "Japanese"),
        no_repeat_ngram_size=int(gen_cfg.get("no_repeat_ngram_size", 0)),
        repetition_penalty=float(gen_cfg.get("repetition_penalty", 1.0)),
    )

    engine_cache: dict = {}

    def engine_factory(progress):
        if "engine" not in engine_cache:
            engine_cache["engine"] = AnimeWhisperEngine(settings, progress=progress)
        else:
            engine_cache["engine"]._progress = progress
        return engine_cache["engine"]

    results = []
    for media in media_files:
        try:
            results.append(
                process_file(
                    media,
                    engine_factory=engine_factory,
                    cfg=cfg,
                    args=args,
                    output_dir=resolve_output_dir(media, output_root, input_root),
                )
            )
        except KeyboardInterrupt:
            logging.warning("用户中断")
            break
        except Exception as exc:  # noqa: BLE001
            logging.exception("处理失败：%s", media.name)
            results.append({"file": str(media), "status": "failed", "error": f"{type(exc).__name__}: {exc}"})

    # 汇总
    logging.info("=" * 68)
    logging.info("汇总：")
    for r in results:
        if r["status"] == "done":
            logging.info(
                "  ✅ %s → %d 条字幕（%.1fs，速度 %.1fx 实时）",
                r["file"],
                r.get("lines", 0),
                r.get("elapsed_s", 0),
                r.get("duration_s", 0) / max(1e-6, r.get("elapsed_s", 1)),
            )
        elif r["status"] == "skipped":
            logging.info("  ⏭️  %s（已存在，跳过）", r["file"])
        else:
            logging.info("  ❌ %s → %s", r["file"], r.get("error"))
    logging.info("产物根目录：%s", output_root)

    return 0 if all(r["status"] in ("done", "skipped") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
