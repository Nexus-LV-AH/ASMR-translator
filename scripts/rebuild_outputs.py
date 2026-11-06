#!/usr/bin/env python3
"""从翻译缓存重建中文产物（.zh.txt / .lrc / .zh.meta.json），**不调用模型**。

用途：
    - 产物被误删 / 误覆盖时恢复（缓存里存着完整的"日文→中文"对照）
    - 只改了输出格式（如 lrc 头部）时，无需重跑模型即可重写产物

与 translate.py 对齐：同目录存在 <轨道>.ja.refined.txt 时优先用它，因为
缓存键是"翻译当时那一版日文行"的文本，台本精修后必须用精修版才命中。

安全阀：重建前先体检，任何文件缓存未命中比例 > --max-missing-ratio（默认 2%）
就整体拒绝写入（返回码 3），避免把日文原文混进 .zh.txt。

用法：
    .venv/bin/python scripts/rebuild_outputs.py                    # 全部（含 LRC）
    .venv/bin/python scripts/rebuild_outputs.py --only-zh-txt      # 保护手改过的 LRC
    .venv/bin/python scripts/rebuild_outputs.py --work 本編(wav)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import translate as T  # noqa: E402

logger = logging.getLogger("rebuild")


def rebuild_one(ja_path: Path, out_dir: Path, lrc_dir: Path, only_zh_txt: bool = False,
                cfg: dict | None = None) -> dict:
    """从缓存重写 <stem>.zh.txt（必要时也重写 LRC）。out_dir/lrc_dir 直接给出，兼容工作区与作品包两种布局。"""
    stem = T.subtitle_stem(ja_path)
    cache_path = out_dir / f"{stem}.zh.cache.json"
    if not cache_path.exists():
        return {"file": ja_path.name, "status": "no-cache"}

    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    lines = T.parse_ja_txt(ja_path)
    if not lines:
        return {"file": ja_path.name, "status": "empty"}

    zh: list[str] = []
    missing = 0
    cleaned = 0
    polluted = 0
    for l in lines:
        raw = cache.get(l.text)
        if raw is None:
            missing += 1
            zh.append(l.text)          # 未命中缓存 → 保留日文原文
            continue
        t, leaked = T.scrub_scaffolding(T.strip_output_wrapper(raw))
        if leaked:
            # 提示词脚手架污染过的译文：标签后面其实是**下一行**的译文（错行内容），
            # 宁可回退日文原文，也不要把错行内容写进产物；下次 translate 会自动重译这些行
            polluted += 1
            zh.append(l.text)
            continue
        if t != raw:
            cleaned += 1
        zh.append(t if t else l.text)  # 整行都是提示词脚手架 → 回退日文原文
    title = T.extract_title(stem)
    title_zh = cache.get(f"__title__:{title}")

    cfg = cfg if cfg is not None else T.load_config(PROJECT_ROOT / "config.yaml")
    oc = dict((cfg.get("translation") or {}).get("output") or {})
    # 重建时保持原来的来源标签（meta 里记录过模型）；否则 [by:] 会退回旧默认值
    meta_path = out_dir / f"{stem}.zh.meta.json"
    if meta_path.exists():
        try:
            model = str(json.loads(meta_path.read_text(encoding="utf-8")).get("model") or "")
            if model:
                oc["by_tag"] = T.model_by_tag(cfg, model)
        except Exception:  # noqa: BLE001
            pass
    if only_zh_txt:
        oc["write_lrc"] = False        # 保护手动改过的 LRC，只重写中文文本
    written = T.write_outputs(stem, lines, zh, out_dir, oc, title_zh=title_zh, lrc_dir=lrc_dir)

    meta = {
        "file": ja_path.name,
        "lines": len(lines),
        "rebuilt_from_cache": True,
        "scaffolding_lines_cleaned": cleaned,
        "scaffolding_lines_dropped": polluted,
        "lines_not_in_cache": missing,
        "title_zh": title_zh,
        "outputs": {k: str(v) for k, v in written.items()},
    }
    (out_dir / f"{stem}.zh.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("  %s → 重建 %d 行（缓存缺 %d 行，清理脚手架 %d 行，作废污染译文 %d 行）",
                stem, len(lines), missing, cleaned, polluted)
    return {"file": ja_path.name, "status": "done", "lines": len(lines),
            "missing": missing, "polluted": polluted}


def audit_one(ja_path: Path, zh_dir: Path, subtitles_root: Path | None = None) -> dict:
    """只读检查：缓存里有几行、当前日文有几行对不上。不写任何文件。zh_dir 直接给出。"""
    stem = T.subtitle_stem(ja_path)
    cache_path = zh_dir / f"{stem}.zh.cache.json"
    if not cache_path.exists():
        return {"file": ja_path.name, "status": "no-cache", "lines": 0, "missing": 0}
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    lines = T.parse_ja_txt(ja_path)
    missing = sum(1 for l in lines if l.text not in cache)
    return {"file": ja_path.name, "status": "done", "lines": len(lines), "missing": missing}


def main() -> int:
    p = argparse.ArgumentParser(description="从缓存重建中文产物（不调用模型）")
    p.add_argument("--config", default=str(PROJECT_ROOT / "config.yaml"), help="配置文件路径")
    p.add_argument("--subtitles", default=None)
    p.add_argument("--output-root", default=None)
    p.add_argument("--lrc-root", default=None)
    p.add_argument("--work", default=None, help="只重建指定作品")
    p.add_argument("--only-zh-txt", action="store_true",
                   help="只重写中文文本（.zh.txt），不动 LRC——用于保护手动修改过的 LRC")
    p.add_argument("--no-refined", action="store_true",
                   help="忽略台本精修版（<轨道>.ja.refined.txt），只用 ASR 原文（一般不需要）")
    p.add_argument("--max-missing-ratio", type=float, default=0.02,
                   help="缓存未命中比例上限（默认 0.02）；超过则拒绝重建，避免把日文原文写进产物")
    p.add_argument("--force", action="store_true", help="忽略未命中比例上限，强制重建")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)

    cfg = T.load_config(Path(args.config).expanduser()
                        if Path(args.config).is_absolute() else PROJECT_ROOT / args.config)
    import paths_util

    wp = paths_util.work_paths(cfg)

    def _p(v, default: Path) -> Path:
        if not v:
            return default
        q = Path(str(v)).expanduser()
        return q if q.is_absolute() else PROJECT_ROOT / q

    sub_root = _p(args.subtitles, wp["ja"])
    out_root = _p(args.output_root, wp["zh"])
    lrc_root = _p(args.lrc_root, wp["lrc"])
    for d in (sub_root, out_root):
        if not d.is_dir():
            logger.error("目录不存在：%s", d)
            return 2

    files = sorted(sub_root.rglob("*.ja.txt"))
    if args.work:
        files = [f for f in files if args.work in str(f)]
    # 与 translate.py 保持一致：同目录存在 <轨道>.ja.refined.txt 时优先使用精修版。
    # 缓存键是"翻译当时那一版的日文行文本"，台本精修后键取自精修版，
    # 若这里仍读 ASR 原文就会出现大量"缓存未命中"并把日文原文写回 .zh.txt。
    if not args.no_refined:
        picked = []
        for f in files:
            rf = f.parent / (T.subtitle_stem(f) + ".ja.refined.txt")
            if rf.exists():
                picked.append(rf)
            else:
                picked.append(f)
        files = picked
        if any(f.name.endswith(".ja.refined.txt") for f in files):
            logger.info("检测到台本精修版，优先使用（与 translate.py 一致）")

    # 归档后的作品包也支持：output/<作品>/字幕文件/日文/*.ja.txt → 写回 字幕文件/中文/
    targets: list[tuple[Path, Path, Path]] = []
    for f in files:
        try:
            rel = f.resolve().parent.relative_to(sub_root.resolve())
        except ValueError:
            rel = Path(".")
        targets.append((f, out_root / rel, lrc_root / rel))
    _bdirs = cfg.get("bundle", {}).get("dirs") or {}
    _subs = str(_bdirs.get("subs") or "字幕文件")
    _ja_name = str(_bdirs.get("ja") or "日文")
    _zh_name = str(_bdirs.get("zh") or "中文")
    bundle_ja = sorted(p for p in out_root.glob(f"*/{_subs}/{_ja_name}") if p.is_dir())
    for ja_dir in bundle_ja:
        work = ja_dir.parent
        if args.work and args.work not in str(work):
            continue
        for f in sorted(ja_dir.rglob("*.ja.txt")):
            if not args.no_refined:
                rf = f.parent / (T.subtitle_stem(f) + ".ja.refined.txt")
                if rf.exists():
                    f = rf
            zh_dir = work / _subs / _zh_name       # LRC 与中文文本同目录
            targets.append((f, zh_dir, zh_dir))
    if bundle_ja:
        logger.info("包含 %d 个已归档作品包（%s）", len(bundle_ja), "、".join(p.parent.name for p in bundle_ja))
    if not targets:
        logger.error("没有找到可重建的对象（%s 下没有 .ja.txt，%s 下也没有作品包）", sub_root, out_root)
        return 2

    logger.info("待重建 %d 个文件（缓存目录：%s）", len(targets), out_root)

    # 先体检：任何文件未命中比例过高都说明"缓存与当前日文不是同一版"，
    # 此时重建只会把日文原文写进 .zh.txt（曾因此损坏过产物）。
    audit = [audit_one(f, d) for f, d, _ in targets]
    bad = [a for a in audit if a["status"] == "done"
           and a["lines"] and a["missing"] / a["lines"] > args.max_missing_ratio]
    if bad and not args.force:
        logger.error("以下文件缓存未命中比例超限，拒绝重建（避免写入日文原文）：")
        for a in bad:
            logger.error("  %s：%d/%d 行未命中（%.0f%%）",
                         a["file"], a["missing"], a["lines"], 100 * a["missing"] / a["lines"])
        logger.error("缓存与当前日文不同版（常见原因：重新提取过 ASR，或该用台本精修版）。")
        logger.error("确认无误可加 --force；否则请先对相应轨道跑 translate 补齐译文。")
        return 3
    for a in audit:
        if a["status"] == "no-cache":
            logger.warning("  无缓存，跳过：%s", a["file"])
        elif a["missing"]:
            logger.warning("  %s：%d/%d 行未命中缓存（写回日文原文）", a["file"], a["missing"], a["lines"])

    results = [rebuild_one(f, d, l, only_zh_txt=args.only_zh_txt, cfg=cfg) for f, d, l in targets]
    done = [r for r in results if r["status"] == "done"]
    logger.info("=" * 56)
    logger.info("重建完成：%d/%d 个文件，共 %d 行（未命中缓存的行保留日文原文）",
                len(done), len(targets), sum(r.get("lines", 0) for r in done))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
