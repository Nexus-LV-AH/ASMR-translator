"""字幕组装与输出。

- 语音段 + 识别文本 → 字幕行（每段一条）
- 时间戳格式 [HH:MM:SS]（支持 ≥60 分钟音频，规避旧 [MM:SS] 格式的缺陷）
- 输出 <名>.ja.txt（唯一交付物）+ timing.json / meta.json（辅助，供下一阶段台本对照修正）
- 不做任何拟声/吐息剔除（按需求保留）
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_WS_RE = re.compile(r"\s+")


@dataclass
class Line:
    index: int
    start_ms: int
    end_ms: int
    text: str

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text,
        }


def format_timestamp(ms: int) -> str:
    total = max(0, int(round(ms / 1000.0)))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"[{hours:02d}:{minutes:02d}:{seconds:02d}]"


def clean_text(text: str) -> str:
    """只做空白规整：上下文里的换行合并、连续空白压缩、首尾去空白。

    刻意不做任何内容过滤 —— 喘息、拟声、笑声等一律保留。
    """
    if not text:
        return ""
    return _WS_RE.sub(" ", str(text).replace("\n", " ")).strip()


def split_by_punctuation(
    start_ms: int,
    end_ms: int,
    text: str,
    min_chars: int = 8,
    max_chars: int = 60,
) -> list[tuple[int, int, str]]:
    """把一条长字幕按标点切成多条，时间按字符数比例分配（近似时间戳）。

    只在"分段窗口"粒度上做细化：窗口时间边界是真实的，窗口内部的切分时间是按字数的估算，
    配置 output.split_lines: none 可关闭，保留整窗一条字幕。
    """
    pieces = [p.strip() for p in re.split(r"(?<=[。！？!?…♪♥])\s*", text) if p.strip()]
    if len(pieces) <= 1:
        return [(start_ms, end_ms, text)]

    # 过短的碎片并入前一块
    merged: list[str] = []
    for p in pieces:
        if merged and len(merged[-1]) < min_chars:
            merged[-1] = merged[-1] + p
        else:
            merged.append(p)

    # 仍然过长的块，在顿号/逗号处再切
    refined: list[str] = []
    for p in merged:
        if len(p) <= max_chars:
            refined.append(p)
            continue
        buf = ""
        for part in re.split(r"(?<=[、，,])", p):
            if buf and len(buf) + len(part) > max_chars:
                refined.append(buf)
                buf = part
            else:
                buf += part
        if buf:
            refined.append(buf)

    total_chars = sum(len(p) for p in refined) or 1
    span = max(1, end_ms - start_ms)
    out: list[tuple[int, int, str]] = []
    cursor = start_ms
    for i, p in enumerate(refined):
        if i == len(refined) - 1:
            piece_end = end_ms
        else:
            piece_end = cursor + int(round(span * len(p) / total_chars))
            piece_end = max(piece_end, cursor + 1)
        out.append((cursor, piece_end, p))
        cursor = piece_end
    return out


def build_lines(
    segments,
    texts: list[str],
    collapse_repeats: int = 3,
    split_mode: str = "punctuation",
    split_min_chars: int = 8,
    split_max_chars: int = 60,
) -> tuple[list[Line], dict]:
    """把语音段/窗口与识别文本组装成字幕行。

    collapse_repeats: 连续相同文本超过该次数时，只保留前 N 条（防 Whisper 死循环幻觉刷屏）。
                      设为 0 或负数表示关闭该安全网。
    split_mode: punctuation（按标点在窗口内细分成多条）| none（一个窗口一条）
    """
    raw_lines: list[Line] = []
    empty_segments = 0
    split_count = 0

    for seg, text in zip(segments, texts):
        cleaned = clean_text(text)
        if not cleaned:
            empty_segments += 1
            continue
        if split_mode == "punctuation":
            pieces = split_by_punctuation(
                seg.start_ms, seg.end_ms, cleaned, min_chars=split_min_chars, max_chars=split_max_chars
            )
            split_count += max(0, len(pieces) - 1)
            for start, end, piece in pieces:
                raw_lines.append(
                    Line(index=len(raw_lines) + 1, start_ms=start, end_ms=end, text=piece)
                )
        else:
            raw_lines.append(
                Line(index=len(raw_lines) + 1, start_ms=seg.start_ms, end_ms=seg.end_ms, text=cleaned)
            )

    lines, collapsed = _collapse_consecutive_duplicates(raw_lines, collapse_repeats)
    for i, line in enumerate(lines, 1):
        line.index = i

    stats = {
        "total_segments": len(segments),
        "empty_segments": empty_segments,
        "lines": len(lines),
        "split_extra_lines": split_count,
        "collapsed_repeat_lines": collapsed,
    }
    if collapsed:
        logger.warning("折叠了 %d 条连续重复行（疑似幻觉刷屏）", collapsed)
    if empty_segments:
        logger.info("有 %d 个窗口未识别出文本（纯气息/静音段），已跳过", empty_segments)
    return lines, stats


def _collapse_consecutive_duplicates(lines: list[Line], limit: int) -> tuple[list[Line], int]:
    if not limit or limit <= 0:
        return lines, 0
    out: list[Line] = []
    collapsed = 0
    run_text = None
    run_count = 0
    for line in lines:
        if line.text == run_text:
            run_count += 1
        else:
            run_text = line.text
            run_count = 1
        if run_count <= limit:
            out.append(line)
        else:
            collapsed += 1
    return out, collapsed


def render_txt(lines: list[Line]) -> str:
    return "\n".join(f"{format_timestamp(line.start_ms)} {line.text}" for line in lines)


def write_outputs(
    *,
    stem: str,
    lines: list[Line],
    output_dir: Path,
    stats: dict,
    meta: dict,
    write_timing: bool = True,
    write_meta: bool = True,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    txt_path = output_dir / f"{stem}.ja.txt"
    txt_path.write_text(render_txt(lines) + ("\n" if lines else ""), encoding="utf-8")
    written["txt"] = txt_path

    if write_timing:
        timing_path = output_dir / f"{stem}.timing.json"
        payload = {
            "stem": stem,
            "stats": stats,
            "lines": [line.as_dict() for line in lines],
        }
        timing_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written["timing"] = timing_path

    if write_meta:
        meta_path = output_dir / f"{stem}.meta.json"
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        written["meta"] = meta_path

    return written
