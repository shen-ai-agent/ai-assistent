"""Nguồn STT đổi bằng config (Phase 10).

`[stt] engine = "whisper"` (mặc định) dùng các khoá phẳng của [stt] (model, compute_type, device) như trước.
Engine khác: tên trong STT_ENGINES hoặc thẳng "module:Lớp", tham số lấy từ bảng `[stt.<tên>]`.
SttService chỉ gọi `transcribe`, nên engine nào có đúng chữ ký đó là dùng được (test dùng engine giả).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import numpy as np

if TYPE_CHECKING:
    from ..config import Config
    from .engine import Transcript

STT_ENGINES = {
    "whisper": "server.stt.engine:WhisperEngine",
}


@runtime_checkable
class SttEngine(Protocol):
    name: str

    def transcribe(self, audio: np.ndarray, *, language: str = "vi", initial_prompt: str | None = None,
                   hotwords: str | None = None, beam_size: int = 5, **kwargs) -> "Transcript": ...


def stt_engine_class(name: str):
    path = STT_ENGINES.get(name) or (name if ":" in name else None)
    if path is None:
        raise ValueError(f"Không có engine STT '{name}' (có: {', '.join(STT_ENGINES)})")
    module, cls = path.split(":")
    return getattr(importlib.import_module(module), cls)


def create_stt_engine(cfg: "Config"):
    cls = stt_engine_class(cfg.stt_engine)
    if cfg.stt_engine == "whisper":
        return cls(cfg.model, cfg.compute_type, cfg.device)
    return cls(**cfg.stt_engines.get(cfg.stt_engine, {}))
