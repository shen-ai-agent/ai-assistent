"""VieNeu-TTS v3 Turbo (gói `vieneu`, 48kHz, có stream, giọng có sẵn hoặc clone từ file mẫu).

Cài trong venv riêng _local/venvs/vieneu, server dùng qua worker (`python = ...` trong [tts.vieneu]).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import numpy as np

from .base import EngineCaps, as_mono

ROOT = Path(__file__).resolve().parents[3]


class VieNeuEngine:
    # native_pause_ms: lặng đầu + cuối câu (trung vị) đo bằng bench/tts_bench.py --model vieneu --gpu, 18/11/2026
    caps = EngineCaps(streaming=True, native_pause_ms=227, normalizes_text=True, voice_clone=True)

    def __init__(self, mode: str = "v3turbo", voice: str = "", ref: str = "", device: str = "auto",
                 watermark: bool = True, **sampling):
        """sampling: tham số của infer_stream (temperature, top_k, top_p, repetition_penalty...)."""
        from vieneu import Vieneu
        self.tts = Vieneu(mode, device=device)
        self.sample_rate = self.tts.sample_rate
        self.ref = str(ROOT / ref) if ref and not Path(ref).is_absolute() else ref
        self.voice = voice or ("clone:" + Path(ref).stem if ref else "mặc định")
        self.opts = dict(sampling, apply_watermark=watermark)
        if ref:
            self.opts["ref_audio"] = self.ref
        elif voice:
            self.opts["voice"] = voice

    def describe(self) -> str:
        return f"VieNeu {getattr(self.tts, 'backend', '')}"

    def voices(self) -> list[str]:
        return [v[1] if isinstance(v, tuple) else str(v) for v in self.tts.list_preset_voices()]

    def stream(self, text: str, lang: str) -> Iterator[np.ndarray]:
        for chunk in self.tts.infer_stream(text, **self.opts):
            yield as_mono(chunk)
