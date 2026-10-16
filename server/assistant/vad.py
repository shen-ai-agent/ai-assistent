"""VAD Silero theo luồng và bộ cắt đoạn nói cho chế độ rảnh tay.

Dùng lại model silero_vad_v6.onnx của faster-whisper (CPU, ~0.1ms mỗi cửa sổ 32ms), nên GPU chỉ chạy
khi đã có một đoạn nói trọn vẹn. Cách gọi model giống faster_whisper.vad.SileroVADModel: mỗi cửa sổ
512 mẫu ghép thêm 64 mẫu cuối của cửa sổ trước, trạng thái LSTM (h, c) mang sang lần gọi sau.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16000
WINDOW = 512        # 32ms
CONTEXT = 64
WINDOW_MS = WINDOW * 1000 // SAMPLE_RATE


class StreamingVad:
    """Xác suất có tiếng nói cho từng cửa sổ 32ms, audio đưa vào dần với độ dài bất kỳ."""

    def __init__(self, session=None):
        if session is None:
            from faster_whisper.vad import get_vad_model
            session = get_vad_model().session
        self.session = session
        self.reset()

    def reset(self):
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.float32)

    def push(self, audio: np.ndarray) -> list[float]:
        buf = np.concatenate([self._pending, audio.astype(np.float32, copy=False)])
        n = len(buf) // WINDOW
        self._pending = buf[n * WINDOW:]
        if n == 0:
            return []
        windows = buf[: n * WINDOW].reshape(n, WINDOW)
        contexts = np.vstack([self._context, windows[:-1, -CONTEXT:]]) if n > 1 else self._context[None]
        self._context = windows[-1, -CONTEXT:].copy()
        # Model coi trục batch là trục thời gian (giống SileroVADModel), nên chạy cả lô một lần
        out, self._h, self._c = self.session.run(
            None, {"input": np.hstack([contexts, windows]), "h": self._h, "c": self._c})
        return [float(p) for p in np.asarray(out).reshape(-1)]


@dataclass
class Segment:
    """Một đoạn nói đã cắt xong. start/end tính bằng mẫu từ lúc bật rảnh tay (đồng hồ của mic)."""
    audio: np.ndarray
    start: int
    end: int
    speech_ms: int
    forced: bool = False   # bị cắt vì quá dài, không phải vì im lặng

    @property
    def audio_ms(self) -> int:
        return round(len(self.audio) * 1000 / SAMPLE_RATE)


class Segmenter:
    """Cắt đoạn nói theo VAD có trễ (hysteresis): bắt đầu khi p ≥ threshold, kết thúc khi p < neg_threshold
    liên tục silence_ms. silence_ms đổi được giữa chừng (lúc chờ tên ngắn hơn lúc nghe câu hỏi).

    push() trả các sự kiện: ("start", idx) khi bắt đầu có tiếng nói, ("end", Segment) khi cắt xong,
    ("drop", idx) khi đoạn quá ít tiếng nói (tiếng gõ phím, ho).
    """

    def __init__(self, vad: StreamingVad, *, threshold: float = 0.5, neg_threshold: float = 0.35,
                 silence_ms: int = 500, min_speech_ms: int = 250, preroll_ms: int = 300,
                 tail_ms: int = 200, max_s: float = 30):
        self.vad = vad
        self.threshold, self.neg_threshold = threshold, neg_threshold
        self.silence_ms = silence_ms
        self.min_speech_ms = min_speech_ms
        self.preroll = preroll_ms * SAMPLE_RATE // 1000 // WINDOW
        self.tail = tail_ms * SAMPLE_RATE // 1000 // WINDOW
        self.max_windows = int(max_s * SAMPLE_RATE) // WINDOW
        self.reset()

    def reset(self):
        self.vad.reset()
        self.pos = 0                 # số mẫu đã nhận (cả phần đang chờ đủ cửa sổ)
        self._win_idx = 0            # số cửa sổ đã qua VAD
        self._raw = np.zeros(0, dtype=np.float32)
        self._ring: deque[np.ndarray] = deque(maxlen=max(self.preroll, 1))
        self._active: list[np.ndarray] | None = None
        self._start = 0
        self._speech = 0
        self._silence = 0

    @property
    def in_speech(self) -> bool:
        return self._active is not None

    def push(self, audio: np.ndarray) -> list[tuple]:
        self.pos += len(audio)
        buf = np.concatenate([self._raw, audio.astype(np.float32, copy=False)])
        n = len(buf) // WINDOW
        self._raw = buf[n * WINDOW:]
        probs = self.vad.push(buf[: n * WINDOW])
        events: list[tuple] = []
        for i, p in enumerate(probs):
            events += self._step(buf[i * WINDOW:(i + 1) * WINDOW], p)
        return events

    def _step(self, win: np.ndarray, p: float) -> list[tuple]:
        idx = self._win_idx
        self._win_idx += 1
        if self._active is None:
            if p >= self.threshold:
                self._active = [*self._ring, win] if self.preroll else [win]
                self._start = (idx - len(self._active) + 1) * WINDOW
                self._ring.clear()
                self._speech, self._silence = 1, 0
                return [("start", self._start)]
            if self.preroll:
                self._ring.append(win)
            return []
        self._active.append(win)
        if p >= self.threshold:
            self._speech += 1
            self._silence = 0
        elif p < self.neg_threshold:
            self._silence += 1
        else:
            self._silence = 0  # vùng lưng chừng: chưa tính là im lặng
        if self._silence * WINDOW_MS >= self.silence_ms:
            return [self._close(forced=False)]
        if len(self._active) >= self.max_windows:
            return [self._close(forced=True)]
        return []

    def _close(self, forced: bool) -> tuple:
        wins = self._active
        if not forced:
            wins = wins[: len(wins) - max(self._silence - self.tail, 0)]  # bỏ phần im lặng, chừa tail_ms
        self._active = None
        speech_ms = self._speech * WINDOW_MS
        if speech_ms < self.min_speech_ms:
            return ("drop", self._start)
        audio = np.concatenate(wins)
        return ("end", Segment(audio, self._start, self._start + len(audio), speech_ms, forced))
