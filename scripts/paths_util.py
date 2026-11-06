#!/usr/bin/env python3
"""统一路径解析：中间目录 + 各子目录。

设计：**一个中间目录**（默认 `中间文件/`），提取字幕写入它、翻译从它读取，
所有中间产物都收在同一个目录里，名字直白（日文字幕/中文字幕/LRC），归档后可整体清空。

    中间文件/
    ├── 日文字幕/     ← 提取输出 = 翻译输入（含 timing/meta/精修版）
    ├── 中文字幕/     ← 翻译输出（.zh.txt / 缓存 / 元数据）
    └── LRC/          ← LRC 暂存，归档时并入作品包的「字幕文件/中文」
"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORK_DIRS = {"ja": "日文字幕", "zh": "中文字幕", "lrc": "LRC"}


def _abs(raw, default: str) -> Path:
    p = Path(str(raw or default)).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def work_paths(cfg: dict) -> dict[str, Path]:
    """解析 config.paths，返回所有根目录（work 及其子目录 + 输入/输出/台本/成品库）。"""
    p = (cfg or {}).get("paths") or {}
    work = _abs(p.get("work_dir"), "中间文件")
    wd = dict(DEFAULT_WORK_DIRS)
    wd.update({k: str(v) for k, v in (p.get("work_dirs") or {}).items() if v})
    return {
        "input": _abs(p.get("input_dir"), "input"),
        "work": work,
        "ja": work / wd["ja"],
        "zh": work / wd["zh"],
        "lrc": work / wd["lrc"],
        "promptbook": _abs(p.get("promptbook_dir"), "promptbook"),
        "output": _abs(p.get("output_dir"), "output"),
        "collect": _abs(p.get("collect_dir"), "LRC成品库"),
    }


# ---------------- 字幕输入识别 ----------------
# 可作为「翻译输入」的字幕：本工具的 .ja.txt / 精修版、通用 .srt、日文 .lrc、带时间戳的 .txt
SUBTITLE_INPUT_RE = re.compile(r"\.(?:ja\.refined\.txt|ja\.txt|srt|lrc|txt)$", re.I)
# 非输入：中文产物与旁挂元数据
_NON_INPUT_RE = re.compile(
    r"(?:\.zh\.txt|\.zh\.cache\.json|\.meta\.json|\.timing\.json|\.ja\.refined\.json)$", re.I)


def is_subtitle_input(path: Path) -> bool:
    """是不是可作为翻译输入的日文字幕文件（排除中文产物/缓存/元数据）。"""
    name = path.name
    if _NON_INPUT_RE.search(name):
        return False
    return bool(SUBTITLE_INPUT_RE.search(name))


def strip_subtitle_suffix(name: str) -> str:
    """去掉字幕后缀，得到轨道名：x.ja.txt / x.ja.refined.txt / x.srt / x.lrc / x.txt → x。"""
    return SUBTITLE_INPUT_RE.sub("", name)


def iter_subtitles(root: Path, *, recursive: bool = True, include_refined: bool = False) -> list[Path]:
    """列出目录下的字幕输入文件（默认不含 .ja.refined.txt，调用方按需配对）。"""
    if not root.is_dir():
        return []
    files = sorted(root.rglob("*") if recursive else root.glob("*"))
    out = []
    for f in files:
        if not f.is_file() or not is_subtitle_input(f):
            continue
        if not include_refined and f.name.endswith(".ja.refined.txt"):
            continue
        out.append(f)
    return out
