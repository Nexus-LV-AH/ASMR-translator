"""音频解码：使用 faster-whisper 内置的 PyAV 解码器，不依赖系统 ffmpeg。

支持 mp3 / wav / flac / m4a / aac / ogg / opus 等（凡 PyAV 能解的都行）。
输出统一为 16kHz 单声道 float32 numpy 数组。
"""
from __future__ import annotations

import numpy as np

SAMPLING_RATE = 16000

SUPPORTED_EXTS = {
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus",
    ".wma", ".mp4", ".webm", ".mkv", ".aiff", ".aif",
}


def is_media_file(path) -> bool:
    from pathlib import Path

    return Path(path).suffix.lower() in SUPPORTED_EXTS


def decode_audio(path, sampling_rate: int = SAMPLING_RATE) -> np.ndarray:
    """解码任意音频为 16k 单声道 float32。

    faster_whisper.audio.decode_audio 内部用 PyAV，自带解码器，无需 ffmpeg 可执行文件。
    """
    from faster_whisper.audio import decode_audio as _fw_decode

    audio = _fw_decode(str(path), sampling_rate=sampling_rate)
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim > 1:  # 兜底：多声道取平均
        audio = audio.mean(axis=1).astype(np.float32)
    return np.ascontiguousarray(audio)


def duration_seconds(audio: np.ndarray, sampling_rate: int = SAMPLING_RATE) -> float:
    return float(len(audio)) / float(sampling_rate)
