"""OmniVoice fine-tune tiếng Việt (gói `omnivoice`). Không stream: cả câu sinh xong mới có audio.

Không có giọng cố định nên clone từ file mẫu `ref`. Cài trong venv riêng _local/venvs/omnivoice, server dùng
qua worker (`python = ...` trong [tts.omnivoice]).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import numpy as np

from .base import EngineCaps, as_mono

ROOT = Path(__file__).resolve().parents[3]


class OmniVoiceEngine:
    REPO = "splendor1811/omnivoice-vietnamese"
    # native_pause_ms: lặng đầu + cuối câu (trung vị) đo bằng bench/tts_bench.py --model omnivoice --gpu, 18/11/2026
    caps = EngineCaps(streaming=False, native_pause_ms=386, voice_clone=True)

    def __init__(self, ref: str = "_local/tts/vieneu/c01.wav", ref_text: str = "", steps: int = 16,
                 device: str = "cuda:0", repo: str = REPO):
        import torch
        from omnivoice import OmniVoice, OmniVoiceGenerationConfig
        self.model = OmniVoice.from_pretrained(repo, device_map=device, dtype=torch.float16)
        self.sample_rate = self.model.sampling_rate
        ref_path = Path(ref) if Path(ref).is_absolute() else ROOT / ref
        self.voice = "clone:" + ref_path.stem
        # Không có ref_text thì model tự nhận dạng file mẫu
        self.prompt = self.model.create_voice_clone_prompt(ref_audio=str(ref_path), ref_text=ref_text or None)
        self.config = OmniVoiceGenerationConfig(num_step=steps)

    def describe(self) -> str:
        return f"OmniVoice, {self.config.num_step} bước"

    def voices(self) -> list[str]:
        return [self.voice]

    def stream(self, text: str, lang: str) -> Iterator[np.ndarray]:
        yield as_mono(self.model.generate(text=text, language="vietnamese" if lang == "vi" else "english",
                                          voice_clone_prompt=self.prompt, generation_config=self.config)[0])
