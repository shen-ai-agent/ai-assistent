"""Interface chung cho mọi nguồn TTS (Phase 10)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterator, Protocol, runtime_checkable

import numpy as np

# normalize.PAUSES được chỉnh theo ZeroTTS, model này tự để ~180ms lặng giữa hai câu (_local/tts_gap_probe.py).
# Engine có lặng sẵn khác thì TtsService bù phần chênh để khoảng nghỉ nghe được vẫn như cũ.
REF_NATIVE_PAUSE_MS = 180


@dataclass(frozen=True)
class EngineCaps:
    streaming: bool = True          # có mẩu audio trước khi đọc hết câu
    native_pause_ms: int = 0        # lặng sẵn đầu + cuối câu (ms), đo khi thêm engine
    normalizes_text: bool = False   # model tự đọc số/viết tắt (vẫn qua normalize để thống nhất cách đọc)
    voice_clone: bool = False       # nhận file giọng mẫu

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "EngineCaps":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@runtime_checkable
class TtsEngine(Protocol):
    """Một câu → các mẩu float32 1-D ở `sample_rate`. Đóng generator giữa chừng thì engine phải dừng sớm."""

    sample_rate: int
    voice: str
    caps: EngineCaps

    def voices(self) -> list[str]: ...

    def stream(self, text: str, lang: str) -> Iterator[np.ndarray]: ...


def as_mono(chunk) -> np.ndarray:
    """Mẩu audio của các thư viện (list, tensor, mảng 2-D) → float32 1-D."""
    if hasattr(chunk, "detach"):
        chunk = chunk.detach().float().cpu().numpy()
    return np.asarray(chunk, dtype=np.float32).reshape(-1)
