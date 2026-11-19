"""Tổng hợp giọng: chuẩn hoá text rồi đưa từng câu cho engine (server/tts/engines, chọn bằng [tts] engine)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

import numpy as np

from ..stt.service import _Watched
from .engines import REF_NATIVE_PAUSE_MS
from .engines.zerotts import ZeroTtsEngine  # noqa: F401  (bench cũ import từ đây)
from .normalize import LEXICON_PATH, Lexicon, Segment, para_break, pause_ms, prepare

log = logging.getLogger("tts")


class TtsService:
    """Chuẩn hoá + tổng hợp. Tự đọc lại lexicon khi file đổi, giống glossary của STT."""

    def __init__(self, engine, *, lexicon_path: Path = LEXICON_PATH, loanwords: bool = False, pause_scale: float = 1.0):
        self.engine = engine
        self.loanwords = loanwords
        self.pause_scale = pause_scale
        # PAUSES chỉnh theo lặng sẵn của ZeroTTS: engine khác lặng ít/nhiều hơn thì bù phần chênh vào mỗi chỗ nghỉ
        # để khoảng nghỉ nghe được không đổi (pause_scale = 0: để nguyên như model sinh)
        caps = getattr(engine, "caps", None)
        self.pause_extra = REF_NATIVE_PAUSE_MS - caps.native_pause_ms if caps is not None and pause_scale > 0 else 0
        self._lexicon = _Watched(lexicon_path, Lexicon.load)

    @property
    def sample_rate(self) -> int:
        return self.engine.sample_rate

    def plan(self, text: str) -> list[Segment]:
        segs = prepare(text, self._lexicon.get(), loanwords=self.loanwords, pause_scale=self.pause_scale)
        if self.pause_extra:
            for seg in segs:
                seg.pause_ms = max(0, seg.pause_ms + self.pause_extra)
        return segs

    def seam_pause(self, before: str, after: str) -> int:
        """Mẩu text sau bắt đầu ở đoạn mới (dòng trống nằm giữa hai mẩu feeder cắt ra): nghỉ như chuyển đoạn."""
        if not para_break(before, after):
            return 0
        return max(0, pause_ms("para", after, self.pause_scale) + self.pause_extra)

    def stream(self, seg: Segment) -> Iterator[np.ndarray]:
        return self.engine.stream(seg.text, seg.lang)


def to_pcm16(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
