"""Tổng hợp giọng (Phase 3): ZeroTTS chạy ONNX trên GPU, stream từng câu.

Chọn theo benchmark Phase 3 (_local/tts_report.md): điểm nghe ngang OmniVoice, TTFA ~80ms, RTF ~0.5, VRAM ~2GB.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

import numpy as np

from ..stt.service import _Watched
from .normalize import LEXICON_PATH, Lexicon, Segment, prepare

log = logging.getLogger("tts")


class ZeroTtsEngine:
    REPO = "zeroweight-ai/ZeroTTS"

    def __init__(self, voice: str = "maichi", device: str = "cuda", threads: int = 4):
        providers = ["CPUExecutionProvider"]
        if device == "cuda":
            # onnxruntime-gpu dùng cuDNN/cuBLAS có sẵn trong torch/lib; phải nạp torch trước khi tạo session
            import torch  # noqa: F401
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        from zerotts import ZeroTTS
        self.tts = ZeroTTS.from_pretrained(self.REPO, providers=providers, intra_op_num_threads=threads)
        self.sample_rate = self.tts.sample_rate
        self.voice = voice
        self.provider = self.tts.prefix_step_sess.get_providers()[0]
        if device == "cuda" and self.provider != "CUDAExecutionProvider":
            # onnxruntime-gpu sai bản CUDA (vd 1.31 cần CUDA 13) thì tự rơi về CPU mà không báo lỗi
            log.warning("ZeroTTS đang chạy %s, không phải GPU: RTF ~1, dễ khựng. Kiểm bản onnxruntime-gpu.",
                        self.provider)

    def voices(self) -> list[str]:
        return self.tts.list_voices()

    def stream(self, text: str, lang: str) -> Iterator[np.ndarray]:
        """Một câu → các mẩu float32 1-D. Mẩu đầu rất ngắn (~80ms audio) để có tiếng sớm."""
        for chunk in self.tts.synthesize_stream(text, voice=self.voice):
            yield np.asarray(chunk, dtype=np.float32).reshape(-1)


class TtsService:
    """Chuẩn hoá + tổng hợp. Tự đọc lại lexicon khi file đổi, giống glossary của STT."""

    def __init__(self, engine, *, lexicon_path: Path = LEXICON_PATH, loanwords: bool = False):
        self.engine = engine
        self.loanwords = loanwords
        self._lexicon = _Watched(lexicon_path, Lexicon.load)

    @property
    def sample_rate(self) -> int:
        return self.engine.sample_rate

    def plan(self, text: str) -> list[Segment]:
        return prepare(text, self._lexicon.get(), loanwords=self.loanwords)

    def stream(self, seg: Segment) -> Iterator[np.ndarray]:
        return self.engine.stream(seg.text, seg.lang)


def to_pcm16(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
