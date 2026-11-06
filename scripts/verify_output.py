#!/usr/bin/env python3
"""字幕产出验收检查（可复现验收，不依赖主观听感）。

检查项：
  1. 格式合规：每行 `[HH:MM:SS] 文本`
  2. 时间单调：开始时间不倒退
  3. 覆盖合理：末条时间不超过音频时长（若同目录有 meta.json）
  4. 拟声/喘息保留：纯拟声行数量 > 0（这是本项目的硬性要求：不做剔除）
  5. 反幻觉：连续相同文本不超过阈值
  6. 文本量：行数、总字数、平均行长

用法：
    python scripts/verify_output.py 中间文件/日文字幕/*.ja.txt
    python scripts/verify_output.py 中间文件/日文字幕/            # 检查目录下所有 .ja.txt
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

LINE_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]\s+(.+)$")
KANA_RE = re.compile(r"[ぁ-んァ-ヶｦ-ﾟ]")
# 真假名（不含长音符 ー / 波浪号 ～）：中文译文里出现这些字符说明这行没译出来
REAL_KANA_RE = re.compile(r"[ぁ-ゖァ-ヺ]")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 提示词脚手架/思考内容溢出（模型把提示词框架当译文输出）
SCAFFOLD_HINT_RE = re.compile(
    r"下一行原文|上一行原文|仅供参考|不要翻译|不要重复输出|已定译文|上文对照"
    r"|\[(?:Input|History|Glossary|Output)\]|【(?:台本|输入|输出|上下文)】"
)
LRC_TS_RE = re.compile(r"^\[\d{2}:\d{2}(?:\.\d{1,2})?\]")
SUBTITLE_SUFFIX_RE = re.compile(r"\.(?:ja(?:\.refined)?\.txt|srt|lrc|txt)$", re.I)
# 行首编号/包裹残留（模型把 `[10. 译文]` 这类输出漏进产物）
NUMBER_WRAPPER_RE = re.compile(r"^\[\d\d:\d\d:\d\d\]\s*(?:[\[【（(]\s*)?\d{1,4}\s*[.、)）．:：\]]")

# 纯拟声/喘息行判定：去掉标点与符号后，只由这些假名构成
MOAN_CHARS = set("んぁぅっーはふすあいうえおひへほふぅぁゃゅょゎわｗんンッー〜～♪♥♡！？!?、。，．…‥ 　・" + "ちぢつづじずぐごぱぴぷぺぽ")
PUNCT = set("、。，．…‥♪♥♡ｗ！？!? 　・〜～")


def is_moan_line(text: str) -> bool:
    core = {c for c in text if c not in PUNCT}
    return bool(core) and core <= MOAN_CHARS


def parse_lines(path: Path) -> tuple[list[tuple[int, str]], list[str]]:
    """解析日文字幕行（支持本工具 .ja.txt、.srt、.lrc、带时间戳 .txt）。"""
    if path.suffix.lower() in (".srt", ".lrc") or (
            path.suffix.lower() == ".txt" and not path.name.endswith(".ja.txt")):
        try:
            import translate as _T

            rows = _T.parse_subtitle(path)
        except Exception:  # noqa: BLE001
            rows = []
        if rows:
            return [(r.start_ms // 1000, r.text) for r in rows], []
    entries: list[tuple[int, str]] = []
    bad: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        m = LINE_RE.match(raw)
        if not m:
            bad.append(raw)
            continue
        seconds = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        entries.append((seconds, m.group(4).strip()))
    return entries, bad


def max_repeat_run(texts: list[str]) -> int:
    best = run = 0
    prev = None
    for t in texts:
        run = run + 1 if t == prev else 1
        prev = t
        best = max(best, run)
    return best


def check_file(path: Path, repeat_limit: int = 3) -> dict:
    entries, bad = parse_lines(path)
    texts = [t for _, t in entries]
    starts = [s for s, _ in entries]

    result = {
        "file": path.name,
        "lines": len(entries),
        "chars": sum(len(t) for t in texts),
        "avg_line_chars": round(sum(len(t) for t in texts) / len(texts), 1) if texts else 0,
        "bad_format_lines": len(bad),
        "moan_lines": sum(1 for t in texts if is_moan_line(t)),
        "max_repeat_run": max_repeat_run(texts),
        "monotonic": all(b >= a for a, b in zip(starts, starts[1:])),
        "last_timestamp_s": starts[-1] if starts else 0,
        "issues": [],
    }

    if not entries:
        result["issues"].append("没有任何字幕行")
    if bad:
        result["issues"].append(f"{len(bad)} 行格式不合规，例如：{bad[0][:60]}")
    if not result["monotonic"]:
        result["issues"].append("时间戳出现倒退")
    if result["moan_lines"] == 0:
        result["issues"].append("没有检测到拟声/喘息行（本项目要求保留，请检查是否被过滤）")
    if result["max_repeat_run"] > repeat_limit:
        result["issues"].append(f"存在连续 {result['max_repeat_run']} 次相同文本（疑似幻觉刷屏）")

    meta_path = path.with_name(SUBTITLE_SUFFIX_RE.sub("", path.name) + ".meta.json")
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}
        dur = float(meta.get("audio_duration_s", 0))
        result["audio_duration_s"] = dur
        if dur and result["last_timestamp_s"] > dur + 1:
            result["issues"].append(f"末条时间 {result['last_timestamp_s']}s 超过音频时长 {dur:.0f}s")
        seg = meta.get("segment") or {}
        result["segments"] = seg.get("segments")
        result["skipped_silent"] = seg.get("skipped_silent")
        st = meta.get("stats") or {}
        result["empty_segments"] = st.get("empty_segments")
        result["collapsed_repeat_lines"] = st.get("collapsed_repeat_lines")
        t = meta.get("timing_s") or {}
        result["elapsed_s"] = t.get("total")
        if dur and t.get("total"):
            result["realtime_factor"] = round(dur / float(t["total"]), 2)

    return result


def check_translation(ja_path: Path, lrc_dir: Path | None = None, zh_dir: Path | None = None) -> list[str]:
    """检查配对的中文产物：.zh.txt 日文残留/脚手架、LRC 行数、[ti:] 是否中文、meta 未译行。

    lrc_dir / zh_dir 给出时按独立目录查找；否则退回 .ja.txt 同目录。
    若同目录存在 <轨道>.ja.refined.txt（台本精修版），中文产物实际基于它生成，
    因此行数基准改用精修版，避免误报。
    """
    issues: list[str] = []
    stem = SUBTITLE_SUFFIX_RE.sub("", ja_path.name)
    refined = ja_path.with_name(f"{stem}.ja.refined.txt")
    if refined.exists():
        ja_path = refined   # 行数基准换成精修版
    lrc = (lrc_dir / f"{stem}.lrc") if lrc_dir else ja_path.with_name(f"{stem}.lrc")
    zh_path = (zh_dir / f"{stem}.zh.txt") if zh_dir else ja_path.with_name(f"{stem}.zh.txt")
    meta_path = (zh_dir / f"{stem}.zh.meta.json") if zh_dir else ja_path.with_name(f"{stem}.zh.meta.json")

    if not lrc.exists():
        issues.append(f"缺少配对的 .lrc（还没翻译？查找位置：{lrc}）")
        return issues

    # 中文文本体检：日文残留（未译出）+ 提示词脚手架（模型"思考内容溢出"）
    if zh_path.exists():
        zh_lines = [ln for ln in zh_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        kana = [(i + 1, ln) for i, ln in enumerate(zh_lines) if REAL_KANA_RE.search(ln)]
        scaf = [(i + 1, ln) for i, ln in enumerate(zh_lines) if SCAFFOLD_HINT_RE.search(ln)]
        if kana:
            issues.append(f"{len(kana)} 行仍是日文（第 {[i for i, _ in kana[:8]]} 行），例如：{kana[0][1][:50]}")
        if scaf:
            issues.append(f"{len(scaf)} 行混入提示词脚手架（第 {[i for i, _ in scaf[:8]]} 行），"
                          f"例如：{scaf[0][1][:50]}")
        nums = [(i + 1, ln) for i, ln in enumerate(zh_lines) if NUMBER_WRAPPER_RE.match(ln)]
        if nums:
            issues.append(f"{len(nums)} 行残留编号/包裹（第 {[i for i, _ in nums[:8]]} 行），"
                          f"例如：{nums[0][1][:50]}")
    else:
        issues.append(f"缺少 .zh.txt（查找位置：{zh_path}）")

    ja_lines = [ln for ln in ja_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    lrc_lines = [ln for ln in lrc.read_text(encoding="utf-8").splitlines() if ln.strip()]
    head = lrc_lines[0] if lrc_lines else ""
    ti = head[4:-1] if head.startswith("[ti:") and head.endswith("]") else ""
    # 头部：[ti:] [by:]，外加可选的"标题行"（第一行字幕 = 音频标题，与 [ti:] 同文本）
    body = [ln for ln in lrc_lines if LRC_TS_RE.match(ln)]
    if ti and body and body[0].split("]", 1)[-1].strip() == ti.strip():
        body = body[1:]          # 去掉标题行
    # 允许手工删掉 1~2 行（如把重复的「…」行合并掉）
    if len(body) < len(ja_lines) - 2:
        issues.append(f"LRC 行数不足：{len(body)} < 字幕 {len(ja_lines)}（差 {len(ja_lines) - len(body)} 行）")
    if ti and KANA_RE.search(ti) and not CJK_RE.search(ti):
        issues.append(f"[ti:] 仍是日文：{ti[:40]}")

    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}
        untranslated = meta.get("untranslated_lines") or []
        if untranslated and zh_path.exists():
            # meta 可能是旧版规则写的（把「安心…」这类汉字-only 回显也算未译）；
            # 以**实际产物**为准：只有 .zh.txt 那行真的还带假名才算问题
            zh_lines = [ln for ln in zh_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            untranslated = [i for i in untranslated
                            if 1 <= i <= len(zh_lines) and REAL_KANA_RE.search(zh_lines[i - 1])]
        if untranslated:
            issues.append(f"有 {len(untranslated)} 行未译出（第 {untranslated[:8]} 行）")
    return issues


def main() -> int:
    p = argparse.ArgumentParser(description="字幕产出验收检查")
    p.add_argument("paths", nargs="+", help="字幕 txt 文件或目录（目录会递归扫描）")
    p.add_argument("--repeat-limit", type=int, default=3, help="允许的连续重复次数上限")
    p.add_argument("--check-translation", action="store_true", help="同时检查配对的中文 LRC / 未译行")
    p.add_argument("--subtitles-root", default=None, help="日文字幕根（默认 config 中间目录/日文字幕）")
    p.add_argument("--zh-root", default=None, help="中文文本根（默认 config 中间目录/中文字幕）")
    p.add_argument("--lrc-root", default=None, help="LRC 根（默认 config 中间目录/LRC）")
    p.add_argument("--output-root", default=None, help="作品包根（默认 config paths.output_dir）")
    p.add_argument("--config", default="config.yaml", help="配置文件路径（未显式给根目录时用它取默认值）")
    args = p.parse_args()

    try:
        import translate as _T
        import paths_util

        _p = Path(args.config)
        _cfg = _T.load_config(_p if _p.is_absolute() else Path.cwd() / _p)
        _wp = paths_util.work_paths(_cfg)
    except Exception:  # noqa: BLE001
        _wp = {}

    def _root(raw, key: str, default: str) -> Path:
        q = Path(raw or _wp.get(key) or default).expanduser()
        return q if q.is_absolute() else Path.cwd() / q

    root = _root(args.subtitles_root, "ja", "中间文件/日文字幕")
    zh_root = _root(args.zh_root, "zh", "中间文件/中文字幕")
    lrc_root = _root(args.lrc_root, "lrc", "中间文件/LRC")
    out_root = _root(args.output_root, "output", "output")

    def dir_for(base: Path, ja_file: Path) -> Path | None:
        try:
            rel = ja_file.resolve().parent.relative_to(root.resolve())
        except ValueError:
            return None  # 不在日文字幕根下 → 退回同目录查找
        return base / rel

    _bdirs = (_cfg.get("bundle") or {}).get("dirs") or {}
    _subs = str(_bdirs.get("subs") or "字幕文件")
    _ja_name = str(_bdirs.get("ja") or "日文")
    _zh_name = str(_bdirs.get("zh") or "中文")

    def bundle_roots(ja_file: Path) -> tuple[Path, Path] | None:
        """已归档的作品包：<作品>/字幕文件/日文/x.ja.txt → (中文目录, 中文目录)（LRC 在中文里）。"""
        try:
            rel = ja_file.resolve().relative_to(out_root.resolve())
        except ValueError:
            return None
        if len(rel.parts) >= 4 and rel.parts[1] == _subs and rel.parts[2] == _ja_name:
            work = out_root / rel.parts[0]
            zh = work / _subs / _zh_name
            return zh, zh
        return None

    files: list[Path] = []
    for raw in args.paths:
        path = Path(raw)
        if path.is_dir():
            found = sorted(path.rglob("*.ja.txt"))
            if not found:
                found = sorted(f for f in path.rglob("*.txt") if "台本" not in f.name)
            files.extend(found)
        elif path.exists():
            files.append(path)
        else:
            print(f"⚠️ 找不到 {path}")

    if not files:
        print("没有可检查的字幕文件")
        return 2

    failed = 0
    for f in files:
        r = check_file(f, repeat_limit=args.repeat_limit)
        if args.check_translation:
            br = bundle_roots(f)
            if br:      # 作品包布局：中文文本与 LRC 都在 字幕文件/中文/
                zh_d, lrc_d = br
            else:
                zh_d, lrc_d = dir_for(zh_root, f), dir_for(lrc_root, f)
            r["issues"].extend(check_translation(f, lrc_d, zh_d))
        status = "✅ PASS" if not r["issues"] else "❌ FAIL"
        if r["issues"]:
            failed += 1
        print(f"\n{status}  {f}")
        print(f"    字幕行 {r['lines']} | 总字数 {r['chars']} | 平均行长 {r['avg_line_chars']}")
        print(
            f"    拟声/喘息行 {r['moan_lines']} | 最长连续重复 {r['max_repeat_run']} | 时间单调 {r['monotonic']} | 末条 {r['last_timestamp_s']}s"
        )
        if r.get("audio_duration_s"):
            print(
                f"    音频 {r['audio_duration_s']:.0f}s | 窗口 {r.get('segments')} | 跳过静音 {r.get('skipped_silent')} "
                f"| 空窗 {r.get('empty_segments')} | 折叠重复 {r.get('collapsed_repeat_lines')} "
                f"| 总用时 {r.get('elapsed_s')}s（{r.get('realtime_factor')}x 实时）"
            )
        for issue in r["issues"]:
            print(f"    ⚠️  {issue}")

    print(f"\n{'='*60}\n检查完成：{len(files) - failed}/{len(files)} 通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
