"""Nhận dạng một lượt nói theo cấu hình chọn ở Phase 1: prompt theo chủ đề + corrections.

Glossary và corrections được đọc lại khi file thay đổi, không cần restart server.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .corrections import DEFAULT_PATH as CORRECTIONS_PATH, Corrector
from .glossary import DEFAULT_PATH as GLOSSARY_PATH, Term, build_prompt, detect_topic, load_glossary, topics


@dataclass
class SttResult:
    text: str          # sau corrections
    raw: str           # kết quả gốc của model
    topic: str | None
    topic_auto: bool   # chủ đề do server tự đoán (two_pass)
    stt_ms: int


def speech_ms(audio: np.ndarray) -> int:
    """Tổng thời lượng có tiếng nói theo VAD Silero (CPU, vài ms).

    Whisper gặp audio không có tiếng nói thì bịa câu kiểu YouTube ("Cảm ơn các bạn đã theo dõi...").
    Đo bằng VAD thay vì âm lượng: mic qua trình duyệt có thể bị ù điện 50Hz to gần bằng tiếng nói.
    """
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    stamps = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=300))
    return round(sum(s["end"] - s["start"] for s in stamps) * 1000 / 16000)


class _Watched:
    """Giá trị đọc từ file, tự đọc lại khi mtime đổi."""

    def __init__(self, path: Path, loader):
        self.path, self.loader = path, loader
        self._mtime: float | None = None
        self._value = None

    def get(self):
        mtime = self.path.stat().st_mtime if self.path.exists() else 0.0
        if mtime != self._mtime:
            self._value, self._mtime = self.loader(self.path), mtime
        return self._value


class SttService:
    def __init__(self, engine, *, language: str = "vi", beam_size: int = 5,
                 glossary_path: Path = GLOSSARY_PATH, corrections_path: Path = CORRECTIONS_PATH):
        self.engine = engine
        self.language = language
        self.beam_size = beam_size
        self.glossary_path, self.corrections_path = glossary_path, corrections_path
        self._glossary = _Watched(glossary_path, load_glossary)
        self._corrector = _Watched(corrections_path, Corrector.load)

    def topics(self) -> list[str]:
        return topics(self._glossary.get())

    def glossary(self) -> list[Term]:
        return self._glossary.get()

    def corrector(self) -> Corrector:
        return self._corrector.get()

    def transcribe(self, audio: np.ndarray, topic: str | None = None,
                   glossary: list[Term] | None = None) -> SttResult:
        """topic=None: nhận dạng với prompt mặc định, đoán chủ đề, đoán được thì nhận dạng lại.

        glossary: thay glossary hiện tại (dùng khi thử thuật ngữ mới trước khi duyệt).
        """
        t0 = time.perf_counter()
        glossary = glossary if glossary is not None else self._glossary.get()
        topic_auto = topic is None
        if topic is None:
            res = self._run(audio, build_prompt(glossary, "list"))
            topic = detect_topic(res.text, glossary)
            if topic is not None:
                res = self._run(audio, build_prompt(glossary, "topic", topic))
        else:
            res = self._run(audio, build_prompt(glossary, "topic", topic))
        text = self._corrector.get().apply(res.text)
        return SttResult(text, res.text, topic, topic_auto, round((time.perf_counter() - t0) * 1000))

    def transcribe_plain(self, audio: np.ndarray, prompt: str | None = None) -> tuple[str, int]:
        """Nhận dạng không glossary, không corrections, trả (text, stt_ms). Dùng để tìm tên gọi trợ lý:
        prompt glossary kéo câu về thuật ngữ. prompt: chỉ cấu hình assistant.wake_prompt (đo bằng wake_bench)."""
        t0 = time.perf_counter()
        res = self._run(audio, prompt or None)
        return res.text, round((time.perf_counter() - t0) * 1000)

    def _run(self, audio, prompt):
        return self.engine.transcribe(audio, language=self.language, initial_prompt=prompt,
                                      beam_size=self.beam_size)
