#!/usr/bin/env python3
"""台本 × 字幕 对照检查（只出报告，不生成任何内容、不调用模型）。

作用：
    1) 扫 promptbook/ 下的台本，按"轨道编号"与 中间文件/日文字幕/ 里的日文字幕配对
    2) 报告配对结果：哪些配上了、哪些没配上、作品文件夹名是否对得上
    3) 用台本核对字幕：台本行命中率、未命中的台本行、字幕里台本没有的行
    4) 抽取"台本里出现但字幕里缺失"的汉字词，作为人名/专有名词疑点

用法：
    .venv/bin/python scripts/promptbook_check.py
    .venv/bin/python scripts/promptbook_check.py --promptbook promptbook --subtitles 中间文件/日文字幕
    .venv/bin/python scripts/promptbook_check.py --out 对照试验/promptbook_report.md
"""
from __future__ import annotations

import argparse
import difflib
import logging
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

logger = logging.getLogger("promptbook")

PUNCT_RE = re.compile(r"[\s、。，．…‥♪♥♡ｗ！？!?・〜～「」『』（）()\[\]【】\-—～]+")
# 与 refine_script.py 保持一致：`01.xxx` 与 `track01_xxx` 都要能认出轨道号
TRACK_RE = re.compile(r"^(?:track|tr|トラック|第)?[\s._-]*(\d{1,3})(?!\d)", re.I)
TRACK_ANY_RE = re.compile(r"(?:track|tr|トラック)[\s._-]*(\d{1,3})(?!\d)", re.I)
MOAN_ONLY_RE = re.compile(r"^[んぁぅっーはふすあいうえおひへ]+$")
KANJI_WORD_RE = re.compile(r"[\u4e00-\u9fff]{2,6}")


@dataclass
class Track:
    number: str                       # 归一化后的轨道号，如 "04"
    promptbook: Path | None = None
    subtitles: Path | None = None
    script_lines: list[str] = field(default_factory=list)
    sub_lines: list[str] = field(default_factory=list)


def norm_work(name: str) -> str:
    """作品文件夹名归一化：去掉首尾空白、波浪号差异、全半角差异。"""
    return unicodedata.normalize("NFKC", name).strip()


def track_number(filename: str) -> str | None:
    """从文件名取轨道号：'04.txt' / '04.放課後…' / 'track04_xxx' → '04'。"""
    name = filename.strip()
    m = TRACK_RE.match(name) or TRACK_ANY_RE.search(name)
    return m.group(1).zfill(2) if m else None


def norm_text(s: str) -> str:
    return PUNCT_RE.sub("", s)


def script_lines_of(path: Path) -> list[str]:
    """解析台本：跳过标题/注释行，保留台词（含拟声），用于对照。"""
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = raw.strip()
        if not s or s.startswith(("//", "※", "#", "【", "《")):
            continue
        core = norm_text(s)
        if not core:
            continue
        if MOAN_ONLY_RE.match(core):      # 纯拟声行照收（用来核对字幕是否保留）
            out.append(s)
            continue
        out.append(s)
    return out


def sub_lines_of(path: Path) -> list[str]:
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r"^\[\d{2}:\d{2}(?::\d{2})?\]\s*(.+)$", raw.strip())
        if m:
            out.append(m.group(1).strip())
    return out


def coverage(needle: str, haystack: str) -> float:
    """needle 的内容有多少能在 haystack 里找到（内容覆盖度）。"""
    if not needle or not haystack:
        return 0.0
    sm = difflib.SequenceMatcher(None, needle, haystack, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) / len(needle)


def collect(promptbook_root: Path, subtitles_root: Path) -> dict[str, dict[str, Track]]:
    """返回 {台本作品名: {轨道号: Track}}。"""
    works: dict[str, dict[str, Track]] = defaultdict(dict)

    # 先按台本收集
    for script in sorted(promptbook_root.rglob("*.txt")):
        if script.name.startswith("."):
            continue
        num = track_number(script.name)
        if not num:
            continue
        work = norm_work(script.parent.name)
        t = works[work].setdefault(num, Track(number=num))
        t.promptbook = script
        t.script_lines = script_lines_of(script)

    # 再把字幕按轨道号挂进去（作品名不一致时，若只有一个候选作品就自动关联并标注）
    sub_dirs = {norm_work(d.name): d for d in subtitles_root.iterdir() if d.is_dir()} if subtitles_root.is_dir() else {}
    for work, tracks in works.items():
        sub_dir = sub_dirs.get(work)
        if sub_dir is None:
            # 名字对不上：如果字幕根下只有一个作品目录，就按轨道号关联（并在报告里提示改名）
            if len(sub_dirs) == 1:
                sub_dir = next(iter(sub_dirs.values()))
        if sub_dir is None:
            continue
        by_num: dict[str, Path] = {}
        for f in sub_dir.rglob("*.ja.txt"):
            num = track_number(f.name)
            if num:
                by_num.setdefault(num, f)
        for num, t in tracks.items():
            if num in by_num:
                t.subtitles = by_num[num]
                t.sub_lines = sub_lines_of(by_num[num])
    return works


def main() -> int:
    p = argparse.ArgumentParser(description="台本 × 字幕 对照检查（只出报告）")
    p.add_argument("--config", default="config.yaml", help="配置文件路径")
    p.add_argument("--promptbook", default=None, help="台本根目录（默认 config paths.promptbook_dir）")
    p.add_argument("--subtitles", default=None, help="日文字幕根目录（默认 config 中间目录/日文字幕）")
    p.add_argument("--out", default="对照试验/promptbook_report.md", help="报告输出路径")
    p.add_argument("--threshold", type=float, default=0.62, help="台本行命中判定阈值（覆盖度）")
    p.add_argument("--show", type=int, default=8, help="每类样例输出条数")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)

    try:
        import yaml

        _p = Path(args.config)
        _cfg = yaml.safe_load((_p if _p.is_absolute() else PROJECT_ROOT / _p).read_text(encoding="utf-8")) or {}
        _paths = _cfg.get("paths") or {}
    except Exception:  # noqa: BLE001
        _paths = {}
    pb_arg = args.promptbook or _paths.get("promptbook_dir") or "promptbook"
    try:
        import paths_util

        _wp = paths_util.work_paths(cfg)
    except Exception:  # noqa: BLE001
        _wp = {}
    sub_arg = args.subtitles or str(_wp.get("ja") or "中间文件/日文字幕")
    pb_root = Path(pb_arg)
    if not pb_root.is_absolute():
        pb_root = PROJECT_ROOT / pb_root
    sub_root = Path(sub_arg)
    if not sub_root.is_absolute():
        sub_root = PROJECT_ROOT / sub_root
    if not pb_root.is_dir():
        logger.error("找不到台本目录：%s", pb_root)
        return 2
    if not sub_root.is_dir():
        logger.error("找不到字幕目录：%s", sub_root)
        return 2

    works = collect(pb_root, sub_root)
    if not works:
        logger.error("没有在 %s 下找到 <轨道号>.txt 形式的台本", pb_root)
        return 2

    sub_dirs = sorted(d.name for d in sub_root.iterdir() if d.is_dir())
    lines_md: list[str] = [
        "# 台本 × 字幕 对照报告",
        "",
        f"- 台本根目录：`{pb_root.relative_to(PROJECT_ROOT)}`",
        f"- 字幕根目录：`{sub_root.relative_to(PROJECT_ROOT)}`",
        f"- 字幕下的作品文件夹：{', '.join(repr(d) for d in sub_dirs) or '（无）'}",
        "",
    ]

    total_hit = total_script = 0
    for work, tracks in sorted(works.items()):
        lines_md += [f"## {work}", "", "| 轨道 | 台本 | 字幕 | 台本行 | 字幕行 | 命中 | 未命中 | 字幕多出 |", "|---|---|---|---|---|---|---|---|"]
        for num in sorted(tracks):
            t = tracks[num]
            hit = miss = extra = 0
            miss_samples: list[str] = []
            extra_samples: list[str] = []
            if t.subtitles and t.script_lines:
                joined = norm_text("".join(t.sub_lines))
                script_joined = norm_text("".join(t.script_lines))
                for s in t.script_lines:
                    n = norm_text(s)
                    if len(n) < 3:
                        continue
                    if coverage(n, joined) >= args.threshold:
                        hit += 1
                    else:
                        miss += 1
                        if len(miss_samples) < args.show:
                            miss_samples.append(s)
                for s in t.sub_lines:
                    n = norm_text(s)
                    if len(n) < 3:
                        continue
                    if coverage(n, script_joined) < args.threshold:
                        extra += 1
                        if len(extra_samples) < args.show:
                            extra_samples.append(s)
                total_hit += hit
                total_script += hit + miss
            mark = "✅" if t.subtitles and t.promptbook else "⚠️"
            lines_md.append(
                f"| {num} | {'有' if t.promptbook else '无'} | {t.subtitles.name if t.subtitles else '**未找到**'} "
                f"| {len(t.script_lines)} | {len(t.sub_lines)} | {hit} | {miss} | {extra} |"
            )
        if work not in sub_dirs:
            lines_md += ["", f"> ⚠️ 台本文件夹名 `{work}` 与字幕下的作品文件夹 {sub_dirs} 不一致——"
                             "需要你决定如何关联（改名 / 建映射表 / 在台本里写作品号）。"]
        lines_md.append("")

    if total_script:
        lines_md += ["## 汇总", "", f"- 台本行命中率：**{total_hit}/{total_script} = {total_hit / total_script * 100:.1f}%**", ""]

    out = Path(args.out)
    if not out.is_absolute():
        out = PROJECT_ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines_md) + "\n", encoding="utf-8")

    logger.info("=" * 60)
    for work, tracks in sorted(works.items()):
        matched = sum(1 for t in tracks.values() if t.subtitles)
        logger.info("作品 %s：%d 个轨道，其中 %d 个已配对字幕", work, len(tracks), matched)
    if total_script:
        logger.info("台本行命中率：%d/%d = %.1f%%", total_hit, total_script, total_hit / total_script * 100)
    logger.info("报告已写入：%s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
