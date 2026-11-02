"""ASR 推理引擎：默认 anime-whisper（transformers pipeline，Apple Silicon 走 MPS）。

要点（来自官方模型卡）：
- anime-whisper **不能使用 initial prompt**，否则幻觉严重、质量劣化 —— 本模块不提供 prompt 参数
- 官方推荐 generate kwargs：language=Japanese, no_repeat_ngram_size=0, repetition_penalty=1.0
- 忠实转写吐息/笑声等非言语发声，因此我们不做任何"剔除拟声"的处理
"""
from __future__ import annotations

import dataclasses
import logging
import time
from typing import Callable, Sequence

import numpy as np

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class AsrSettings:
    hf_id: str
    device: str = "auto"
    dtype: str = "float16"
    batch_size: int = 32
    chunk_length_s: float = 30.0
    language: str = "Japanese"
    no_repeat_ngram_size: int = 0
    repetition_penalty: float = 1.0


def resolve_device(requested: str) -> str:
    if requested and requested != "auto":
        return requested
    try:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
    except Exception:  # pragma: no cover - 环境相关
        pass
    return "cpu"


def _resolve_dtype(torch_module, dtype_name: str, device: str):
    if dtype_name == "float16" and device == "mps":
        return torch_module.float16
    return torch_module.float32


class AnimeWhisperEngine:
    """transformers pipeline 封装；支持批量、进度回调、MPS 失败自动回退 CPU。"""

    def __init__(self, settings: AsrSettings, progress: Callable[[int, int], None] | None = None):
        self.settings = settings
        self._progress = progress
        self.device = resolve_device(settings.device)
        self._build(self.device, settings.dtype)

    # ------------------------------------------------------------------ 构建
    def _build(self, device: str, dtype_name: str):
        import torch
        from transformers import pipeline

        dtype = _resolve_dtype(torch, dtype_name, device)
        logger.info(
            "加载模型 %s（device=%s, dtype=%s）...", self.settings.hf_id, device, dtype
        )
        self.pipe = self._make_pipeline(pipeline, device, dtype, batch_size=self.settings.batch_size)
        self.device = device
        self.dtype = dtype
        self.generate_kwargs = {
            "language": self.settings.language,
            "no_repeat_ngram_size": self.settings.no_repeat_ngram_size,
            "repetition_penalty": self.settings.repetition_penalty,
        }
        logger.info("模型加载完成：device=%s dtype=%s", self.device, self.dtype)

    def _make_pipeline(self, pipeline_fn, device: str, dtype, batch_size: int):
        base = {
            "task": "automatic-speech-recognition",
            "model": self.settings.hf_id,
            "device": device,
            "chunk_length_s": self.settings.chunk_length_s,
            "batch_size": batch_size,
        }
        # transformers 新旧版本参数名不同：新版用 dtype，旧版用 torch_dtype
        try:
            return pipeline_fn(**base, dtype=dtype)
        except TypeError:
            return pipeline_fn(**base, torch_dtype=dtype)

    def _fallback_to_cpu(self):
        logger.warning("MPS 推理失败，回退到 CPU/float32 重试（速度会变慢）")
        self._build("cpu", "float32")

    # ------------------------------------------------------------------ 推理
    def transcribe_batch(self, chunks: Sequence[np.ndarray]) -> list[str]:
        chunks = [np.ascontiguousarray(c, dtype=np.float32) for c in chunks]
        if not chunks:
            return []

        texts: list[str] = []
        total = len(chunks)
        block_size = max(1, self.settings.batch_size * 2)

        for start in range(0, total, block_size):
            block = list(chunks[start : start + block_size])
            texts.extend(self._transcribe_block(block))
            if self._progress:
                self._progress(min(start + len(block), total), total)

        return texts

    def _transcribe_block(self, block: list[np.ndarray]) -> list[str]:
        try:
            return self._run_pipeline(block)
        except Exception as exc:  # noqa: BLE001 - MPS 可能抛各类 RuntimeError
            logger.warning("批量推理出错（%s: %s）", type(exc).__name__, exc)
            if self.device != "cpu":
                self._fallback_to_cpu()
                try:
                    return self._run_pipeline(block)
                except Exception as exc2:  # noqa: BLE001
                    logger.warning("CPU 批量推理仍失败（%s），改为逐条推理", exc2)
            return [self._transcribe_single(chunk) for chunk in block]

    def _run_pipeline(self, block: list[np.ndarray]) -> list[str]:
        outputs = self.pipe(block, generate_kwargs=self.generate_kwargs)
        if isinstance(outputs, dict):
            outputs = [outputs]
        return [self._extract_text(item) for item in outputs]

    def _transcribe_single(self, chunk: np.ndarray) -> str:
        try:
            out = self.pipe(chunk, generate_kwargs=self.generate_kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.error("单条推理失败，跳过该段：%s", exc)
            return ""
        return self._extract_text(out)

    @staticmethod
    def _extract_text(item) -> str:
        if isinstance(item, dict):
            return str(item.get("text", "") or "")
        return str(item or "")


def timed(fn):
    """简易计时装饰器（仅用于日志）。"""

    def wrapper(*args, **kwargs):
        t0 = time.time()
        result = fn(*args, **kwargs)
        logger.info("%s 用时 %.1fs", fn.__name__, time.time() - t0)
        return result

    return wrapper
