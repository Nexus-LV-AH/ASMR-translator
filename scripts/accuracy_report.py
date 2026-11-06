#!/usr/bin/env python3
"""用台本核对 ASR 字幕准确率（有台本的作品可用）。

指标说明：
    使用"内容覆盖度"（needle 的字符有多少能在 haystack 中找到匹配块），
    而不是整串相似度 —— ASR 行常带额外喘息前缀（"はぁ、はぁ…"），
    整串相似度会被严重低估（实测同一份产出：整串相似度 65% vs 覆盖度 96%）。

用法：
    python scripts/accuracy_report.py --asr 中间文件/日文字幕/xxx.ja.txt --script track3台本.txt
    python scripts/accuracy_report.py --asr ... --script ... --threshold 0.7
"""
from __future__ import annotations

import argparse
import difflib
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PUNCT_RE = re.compile(r"[\s、。，．…‥♪♥♡ｗ！？!?・〜～「」『』（）()\-—]+")
MOAN_ONLY_RE = re.compile(r"^[んぁぅっーはふすあいうえおひへ]+$")


def norm(s: str) -> str:
    return PUNCT_RE.sub("", s)


def coverage_ratio(needle: str, haystack: str) -> float:
    """needle 的内容有多少能在 haystack 中找到（内容覆盖度）。"""
    if not needle or not haystack:
        return 0.0
    sm = difflib.SequenceMatcher(None, needle, haystack, autojunk=False)
    matched = sum(b.size for b in sm.get_matching_blocks())
    return matched / len(needle)


def load_script(path: Path) -> list[str]:
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("【", "※", "《", "//")):
            continue
        core = norm(line)
        if len(core) < 4 or MOAN_ONLY_RE.match(core):
            continue
        out.append(line)
    return out


def load_asr(path: Path) -> list[str]:
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\[\d{2}:\d{2}:\d{2}\]\s+(.+)$", raw.strip())
        if m:
            out.append(m.group(1))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="用台本核对字幕准确率")
    p.add_argument("--asr", required=True, help="ASR 产出的 .ja.txt")
    p.add_argument("--script", required=True, help="台本 txt")
    p.add_argument("--threshold", type=float, default=0.62, help="判定命中的覆盖度阈值")
    p.add_argument("--show", type=int, default=15, help="每类样例显示条数")
    args = p.parse_args()

    asr_path = Path(args.asr)
    if not asr_path.is_absolute():
        asr_path = PROJECT_ROOT / asr_path
    script_path = Path(args.script)
    if not script_path.is_absolute():
        script_path = PROJECT_ROOT / script_path

    for path in (asr_path, script_path):
        if not path.exists():
            print(f"⚠️ 文件不存在：{path}")
            return 2

    asr_lines = load_asr(asr_path)
    script_lines = load_script(script_path)
    if not asr_lines or not script_lines:
        print(f"⚠️ 数据为空：ASR {len(asr_lines)} 行，台本 {len(script_lines)} 行")
        return 2

    asr_concat = norm("".join(asr_lines))
    script_concat = norm("".join(script_lines))
    print(
        f"台本台词行 {len(script_lines)}（{len(script_concat)} 字） | "
        f"ASR 字幕行 {len(asr_lines)}（{len(asr_concat)} 字）"
    )

    hits, misses = 0, []
    for line in script_lines:
        n = norm(line)
        if len(n) < 3:
            continue
        r = coverage_ratio(n, asr_concat)
        if r >= args.threshold:
            hits += 1
        else:
            misses.append((r, line))
    total = hits + len(misses)
    print(f"\n【台本命中率】{hits}/{total} = {hits / total * 100:.1f}%  （覆盖度阈值 {args.threshold}）")

    extra = []
    for a in asr_lines:
        n = norm(a)
        if len(n) < 3:
            continue
        r = coverage_ratio(n, script_concat)
        if r < args.threshold:
            extra.append((r, a))
    print(f"【ASR 行在台本中无对应】{len(extra)}/{len(asr_lines)} = {len(extra) / len(asr_lines) * 100:.1f}%")

    if misses:
        print(f"\n未命中的台本行（前 {args.show} 条；可能是初稿差异、即兴、或纯拟声）：")
        for r, line in sorted(misses)[: args.show]:
            print(f"  [{r:.2f}] {line}")
    if extra:
        print(f"\nASR 中台本没有的行（前 {args.show} 条；即兴台词或拟声变体）：")
        for r, line in extra[: args.show]:
            print(f"  [{r:.2f}] {line}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
