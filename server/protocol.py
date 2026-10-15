"""Protocol WebSocket giữa web và server. Mọi message định nghĩa ở đây.

Client → Server
  {"type": "start", "topic": "AWS" | null}      bắt đầu lượt nói; topic null = tự đoán (two_pass)
  <binary>                                      PCM int16 little-endian, 16kHz, mono, ~100ms/frame
  {"type": "stop"}                              kết thúc lượt nói, server bắt đầu nhận dạng
  {"type": "cancel"}                            huỷ lượt hiện tại (kể cả khi đang nhận dạng)
  {"type": "correction", "turn_id": "...", "text": "..."}   người dùng sửa tay transcript

Server → Client
  {"type": "hello", "topics": [...], "model": "...", "sample_rate": 16000}
  {"type": "state", "value": "IDLE" | "LISTENING" | "TRANSCRIBING"}
  {"type": "transcript", "turn_id", "text", "topic", "topic_auto", "audio_ms", "stt_ms"}
  {"type": "error", "code": "...", "message": "..."}
"""

from __future__ import annotations

from enum import StrEnum

SAMPLE_RATE = 16000


class State(StrEnum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    TRANSCRIBING = "TRANSCRIBING"


class ErrorCode(StrEnum):
    BAD_MESSAGE = "bad_message"
    BAD_TOPIC = "bad_topic"
    TOO_SHORT = "too_short"
    NO_SPEECH = "no_speech"
    STT_FAILED = "stt_failed"


def hello(topics: list[str], model: str) -> dict:
    return {"type": "hello", "topics": topics, "model": model, "sample_rate": SAMPLE_RATE}


def state(value: State) -> dict:
    return {"type": "state", "value": str(value)}


def transcript(turn_id: str, text: str, topic: str | None, topic_auto: bool,
               audio_ms: int, stt_ms: int) -> dict:
    return {"type": "transcript", "turn_id": turn_id, "text": text, "topic": topic,
            "topic_auto": topic_auto, "audio_ms": audio_ms, "stt_ms": stt_ms}


def error(code: ErrorCode, message: str) -> dict:
    return {"type": "error", "code": str(code), "message": message}
