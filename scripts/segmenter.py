"""分段策略：把音频切成若干"送进模型的窗口"，并给出时间骨架。

实测结论（本机真实 R18 音声，240s 片段）：
- Silero VAD 会把语音从句子中间切断（输出残句如 "…しても和らむように…"），4 分钟仅 122 字
- 固定窗口 12s 得到最完整连贯的文本（640 字，重复度 1.4%），且吻音/喘息全部保留
- anime-whisper 自带时间戳不可用（短窗内只回 (0.48, None) 单点）
因此默认策略是 **固定窗口 + 能量点吸附**（把窗口边界挪到附近的静音谷，避免从词中间切断），
VAD 分段保留为可选策略。
"""
from __future__ import annotations

import dataclasses
import logging

import numpy as np

from vad_segment import Segment, speech_segments, split_long_segments

logger = logging.getLogger(__name__)


def frame_rms_db(audio: np.ndarray, sampling_rate: int = 16000, frame_ms: int = 20) -> tuple[np.ndarray, int]:
    """逐帧 RMS（dBFS）与帧长（样本数）。"""
    frame = max(1, int(frame_ms / 1000 * sampling_rate))
    n_frames = len(audio) // frame
    if n_frames == 0:
        return np.zeros(0, dtype=np.float64), frame
    frames = audio[: n_frames * frame].reshape(n_frames, frame).astype(np.float64)
    rms = np.sqrt(np.mean(frames**2, axis=1) + 1e-12)
    return 20 * np.log10(rms + 1e-12), frame


def noise_floor_db(audio: np.ndarray, sampling_rate: int = 16000) -> float:
    """噪声底：逐帧 RMS 的 10 分位。"""
    db, _ = frame_rms_db(audio, sampling_rate)
    if db.size == 0:
        return -120.0
    return float(np.percentile(db, 10))


def fixed_windows(
    audio: np.ndarray,
    sampling_rate: int = 16000,
    window_ms: int = 12000,
    snap: bool = True,
    snap_search_ms: int = 1500,
    frame_ms: int = 20,
    min_gap_ms: int = 2000,
) -> list[Segment]:
    """固定窗口切分；snap=True 时把每个边界吸附到附近能量最低点（静音谷）。"""
    total_ms = int(len(audio) / sampling_rate * 1000)
    if total_ms <= 0:
        return []

    n_windows = max(1, int(np.ceil(total_ms / window_ms)))
    if n_windows == 1 or not snap:
        return [
            Segment(i * window_ms, min((i + 1) * window_ms, total_ms)) for i in range(n_windows)
        ]

    db, frame = frame_rms_db(audio, sampling_rate, frame_ms)
    boundaries = [i * window_ms for i in range(1, n_windows)]

    snapped: list[int] = []
    prev = 0
    for idx, b in enumerate(boundaries):
        next_b = boundaries[idx + 1] if idx + 1 < len(boundaries) else total_ms
        lo = max(prev + min_gap_ms, b - snap_search_ms, 0)
        hi = min(next_b - min_gap_ms, b + snap_search_ms, total_ms)
        candidate = b
        if hi > lo:
            i0 = max(0, int(lo / frame_ms))
            i1 = min(len(db) - 1, int(hi / frame_ms))
            if i1 > i0:
                candidate = int((i0 + int(np.argmin(db[i0 : i1 + 1]))) * frame_ms)
        candidate = max(candidate, prev + min_gap_ms)
        snapped.append(candidate)
        prev = candidate

    edges = [0] + snapped + [total_ms]
    segments = [Segment(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
    moved = sum(1 for a, b in zip(boundaries, snapped) if a != b)
    logger.info(
        "固定窗口 %dms：%d 段，其中 %d 个边界吸附到静音谷（搜索半径 %.0fms）",
        window_ms,
        len(segments),
        moved,
        snap_search_ms,
    )
    return segments


def gate_silent_segments(
    audio: np.ndarray,
    segments: list[Segment],
    sampling_rate: int = 16000,
    gate_db: float = 6.0,
) -> tuple[list[Segment], int]:
    """丢弃整体能量接近噪声底的窗口（防静音区幻觉，同时省算力）。

    gate_db <= 0 表示关闭该门限。
    """
    if gate_db <= 0 or not segments:
        return segments, 0

    floor = noise_floor_db(audio, sampling_rate)
    threshold = floor + gate_db
    kept: list[Segment] = []
    skipped = 0
    for seg in segments:
        a = int(seg.start_ms / 1000 * sampling_rate)
        b = int(seg.end_ms / 1000 * sampling_rate)
        chunk = audio[a:b]
        if len(chunk) == 0:
            skipped += 1
            continue
        rms = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2) + 1e-12))
        level = 20 * np.log10(rms + 1e-12)
        if level < threshold:
            skipped += 1
            continue
        kept.append(seg)
    if skipped:
        logger.info(
            "静音门限 %.0fdB（噪声底 %.1fdB）：跳过 %d 个近静音窗口，保留 %d 个",
            gate_db,
            floor,
            skipped,
            len(kept),
        )
    return kept, skipped


def build_segments(audio: np.ndarray, cfg: dict, sampling_rate: int = 16000) -> tuple[list[Segment], dict]:
    """按配置构造送模型的窗口。返回 (segments, info)。"""
    cfg = dict(cfg or {})
    mode = str(cfg.get("mode", "fixed")).lower()
    info: dict = {"mode": mode}

    if mode == "vad":
        vad_cfg = dict(cfg.get("vad") or {})
        segments = speech_segments(audio, sampling_rate=sampling_rate, vad_cfg=vad_cfg)
        segments = split_long_segments(
            audio, segments, sampling_rate=sampling_rate, max_ms=int(cfg.get("window_ms", 12000))
        )
    elif mode == "fixed":
        segments = fixed_windows(
            audio,
            sampling_rate=sampling_rate,
            window_ms=int(cfg.get("window_ms", 12000)),
            snap=bool(cfg.get("snap", True)),
            snap_search_ms=int(cfg.get("snap_search_ms", 1500)),
            min_gap_ms=int(cfg.get("min_gap_ms", 2000)),
        )
    else:
        raise SystemExit(f"未知分段模式 {mode!r}（可选：fixed / vad）")

    if not segments:
        logger.warning("分段结果为空，改用整段兜底")
        total_ms = int(len(audio) / sampling_rate * 1000)
        segments = [Segment(0, total_ms)] if total_ms > 0 else []

    segments, skipped = gate_silent_segments(
        audio, segments, sampling_rate=sampling_rate, gate_db=float(cfg.get("silence_gate_db", 6.0))
    )
    info["segments"] = len(segments)
    info["skipped_silent"] = skipped
    return segments, info


__all__ = [
    "Segment",
    "build_segments",
    "fixed_windows",
    "gate_silent_segments",
    "frame_rms_db",
    "noise_floor_db",
]
