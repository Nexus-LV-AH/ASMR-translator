"""Silero VAD 语音分段（用 faster-whisper 内置的 VAD 实现）。

针对耳语音声的调整：
- 低阈值（默认 0.3，Silero 常用 0.5）以抓到悄悄话与吐息
- 相邻段间隙很小时合并成一条字幕
- 超长语音段按能量谷切分，避免单条字幕过长
"""
from __future__ import annotations

import dataclasses
import logging
import os
from typing import Iterable

import numpy as np

# onnxruntime（Silero VAD 的后端）在无法写入缓存目录时，会在**当前目录**丢一个
# ":memory:.ses" 垃圾文件。这里在导入它之前先关掉遥测。
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
os.environ.setdefault("ORT_TELEMETRY_DISABLED", "1")

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class Segment:
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def as_dict(self) -> dict:
        return {"start_ms": self.start_ms, "end_ms": self.end_ms}


def _build_vad_options(vad_cfg: dict):
    """按当前安装的 faster-whisper 版本过滤参数后构造 VadOptions（兼容版本差异）。"""
    from faster_whisper.vad import VadOptions

    supported = {f.name for f in dataclasses.fields(VadOptions)}
    kwargs = {k: v for k, v in vad_cfg.items() if k in supported}
    skipped = sorted(set(vad_cfg) - supported)
    if skipped:
        logger.info("当前 faster-whisper 不支持这些 VAD 参数，已忽略：%s", ", ".join(skipped))
    return VadOptions(**kwargs)


def normalize_for_vad(
    audio: np.ndarray,
    sampling_rate: int = 16000,
    frame_ms: int = 50,
    target_db: float = -30.0,
    max_gain_db: float = 30.0,
    smooth_ms: int = 200,
) -> np.ndarray:
    """仅用于 VAD 的自动增益（AGC）：把低于目标电平的部分提上来，绝不衰减。

    耳语音声里"悄悄话/吐息"可能比正常台词低 20~30dB，Silero 会漏掉；
    但直接把增益后的音频喂给 ASR 会破坏模型输入的分布，
    因此本函数只用于 VAD 检测，转写仍使用原始音频（WhisperJAV 的"enhance for VAD only"思路）。
    """
    frame = max(1, int(frame_ms / 1000 * sampling_rate))
    n_frames = len(audio) // frame
    if n_frames < 2:
        return audio

    frames = audio[: n_frames * frame].reshape(n_frames, frame).astype(np.float64)
    rms = np.sqrt(np.mean(frames**2, axis=1) + 1e-12)

    target = 10 ** (target_db / 20.0)
    max_gain = 10 ** (max_gain_db / 20.0)
    gain = np.clip(target / np.maximum(rms, 1e-9), 1.0, max_gain)

    window = max(1, int(smooth_ms / frame_ms))
    if window > 1:
        gain = np.convolve(gain, np.ones(window) / window, mode="same")
    gain = np.maximum(gain, 1.0)  # 平滑后仍保证只提升不衰减

    centers = (np.arange(n_frames) + 0.5) * frame
    per_sample = np.interp(np.arange(len(audio)), centers, gain)
    boosted = audio.astype(np.float32) * per_sample.astype(np.float32)
    return np.clip(boosted, -1.0, 1.0)


def speech_segments(
    audio: np.ndarray,
    sampling_rate: int = 16000,
    vad_cfg: dict | None = None,
) -> list[Segment]:
    """返回语音段（毫秒），已做间隙合并。"""
    from faster_whisper.vad import get_speech_timestamps

    cfg = dict(vad_cfg or {})
    merge_gap_ms = int(cfg.pop("merge_gap_ms", 250))
    use_agc = bool(cfg.pop("normalize_for_vad", True))
    agc_target_db = float(cfg.pop("agc_target_db", -30.0))
    agc_max_gain_db = float(cfg.pop("agc_max_gain_db", 30.0))

    vad_input = audio
    if use_agc:
        vad_input = normalize_for_vad(
            audio,
            sampling_rate=sampling_rate,
            target_db=agc_target_db,
            max_gain_db=agc_max_gain_db,
        )
        logger.info("VAD 输入已做自动增益（目标 %.0f dB，最大 +%.0f dB）", agc_target_db, agc_max_gain_db)

    options = _build_vad_options(cfg)
    raw = get_speech_timestamps(vad_input, options)

    segs = [
        Segment(
            start_ms=int(round(item["start"] / sampling_rate * 1000)),
            end_ms=int(round(item["end"] / sampling_rate * 1000)),
        )
        for item in raw
    ]
    logger.info("VAD 原始语音段：%d", len(segs))
    merged = merge_short_gaps(segs, merge_gap_ms)
    logger.info("合并短间隙（<=%dms）后：%d", merge_gap_ms, len(merged))
    return merged


def merge_short_gaps(segments: list[Segment], merge_gap_ms: int) -> list[Segment]:
    if not segments:
        return []
    ordered = sorted(segments, key=lambda s: s.start_ms)
    out = [ordered[0]]
    for seg in ordered[1:]:
        last = out[-1]
        if seg.start_ms - last.end_ms <= merge_gap_ms:
            out[-1] = Segment(last.start_ms, max(last.end_ms, seg.end_ms))
        else:
            out.append(seg)
    return out


def split_long_segments(
    audio: np.ndarray,
    segments: list[Segment],
    sampling_rate: int = 16000,
    max_ms: int = 12000,
    window_ms: int = 100,
) -> list[Segment]:
    """把超长段在能量最低处递归切分。"""
    if max_ms <= 0:
        return segments

    out: list[Segment] = []
    for seg in segments:
        out.extend(_split_one(audio, seg, sampling_rate, max_ms, window_ms))
    return out


def _split_one(
    audio: np.ndarray,
    seg: Segment,
    sampling_rate: int,
    max_ms: int,
    window_ms: int,
) -> list[Segment]:
    if seg.duration_ms <= max_ms:
        return [seg]

    cut = _lowest_energy_cut(audio, seg, sampling_rate, window_ms)
    if cut is None:
        return [seg]  # 切不动就原样返回（交给模型处理）

    left = Segment(seg.start_ms, cut)
    right = Segment(cut, seg.end_ms)
    return _split_one(audio, left, sampling_rate, max_ms, window_ms) + _split_one(
        audio, right, sampling_rate, max_ms, window_ms
    )


def _lowest_energy_cut(
    audio: np.ndarray,
    seg: Segment,
    sampling_rate: int,
    window_ms: int,
) -> int | None:
    """在段的中间 50% 区间内找能量最低的窗口，返回切分点（毫秒）。"""
    a = int(seg.start_ms / 1000 * sampling_rate)
    b = int(seg.end_ms / 1000 * sampling_rate)
    b = min(b, len(audio))
    a = max(0, min(a, b))

    win = int(window_ms / 1000 * sampling_rate)
    span = b - a
    if win <= 0 or span < 4 * win:
        return None

    lo = a + span // 4
    hi = b - span // 4
    window = audio[lo:hi]
    n_frames = len(window) // win
    if n_frames < 2:
        return None

    frames = window[: n_frames * win].reshape(n_frames, win)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    best = int(np.argmin(rms))
    cut_sample = lo + (best + 1) * win
    return int(round(cut_sample / sampling_rate * 1000))


def fallback_fixed_windows(
    total_ms: int,
    window_ms: int = 15000,
    hop_ms: int | None = None,
) -> list[Segment]:
    """VAD 什么都没抓到时的兜底：按固定窗口切分，保证仍有输出。"""
    hop = hop_ms or window_ms
    segs: list[Segment] = []
    start = 0
    while start < total_ms:
        end = min(start + window_ms, total_ms)
        segs.append(Segment(start, end))
        if end >= total_ms:
            break
        start += hop
    return segs


def to_dicts(segments: Iterable[Segment]) -> list[dict]:
    return [s.as_dict() for s in segments]
