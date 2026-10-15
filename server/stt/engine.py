"""Nhận dạng giọng nói bằng faster-whisper (CTranslate2)."""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np


def _add_cuda_dlls():
    """CTranslate2 trên Windows cần cublas64_12.dll; bản torch cu12x đã kèm sẵn trong torch/lib."""
    if os.name != "nt":
        return
    spec = importlib.util.find_spec("torch")
    if spec and spec.origin:
        lib = Path(spec.origin).parent / "lib"
        if lib.is_dir():
            os.add_dll_directory(str(lib))
            os.environ["PATH"] = str(lib) + os.pathsep + os.environ.get("PATH", "")


_add_cuda_dlls()

from faster_whisper import WhisperModel  # noqa: E402

MODELS_DIR = Path(__file__).resolve().parents[2] / "models" / "hf"
SAMPLE_RATE = 16000

# Chiến lược chọn ngôn ngữ:
#   vi / en        ép cố định
#   auto           để Whisper tự nhận diện
#   auto_vi_bias   nhận diện trước; chỉ chuyển sang tiếng Anh khi đủ chắc chắn,
#                  còn lại dùng tiếng Việt (câu lẫn Anh–Việt hay bị nhận nhầm là tiếng Anh)
LANGUAGE_MODES = ("vi", "en", "auto", "auto_vi_bias")


@dataclass
class Transcript:
    text: str
    language: str
    language_prob: float


class WhisperEngine:
    def __init__(self, model: str = "large-v3", compute_type: str = "int8_float16", device: str = "cuda"):
        self.name = model
        local = MODELS_DIR.parent / model  # model tự chuyển đổi, vd. models/phowhisper-large-ct2
        if local.is_dir():
            model = str(local)
        self.model = WhisperModel(model, device=device, compute_type=compute_type,
                                  download_root=str(MODELS_DIR))

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        language: str = "vi",
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        beam_size: int = 5,
        en_threshold: float = 0.8,
    ) -> Transcript:
        if language not in LANGUAGE_MODES:
            raise ValueError(f"language phải là một trong {LANGUAGE_MODES}")

        lang, prob = language, 1.0
        if language == "auto_vi_bias":
            detected, _, probs = self.model.detect_language(audio)
            en_prob = dict(probs).get("en", 0.0)
            lang, prob = ("en", en_prob) if en_prob >= en_threshold else ("vi", dict(probs).get("vi", 0.0))

        segments, info = self.model.transcribe(
            audio,
            language=None if lang == "auto" else lang,
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            beam_size=beam_size,
            condition_on_previous_text=False,
            without_timestamps=True,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        if lang == "auto":
            lang, prob = info.language, info.language_probability
        return Transcript(text, lang, prob)
