"""Protocol WebSocket giữa web và server. Mọi message định nghĩa ở đây.

Client → Server
  {"type": "start", "topic": "AWS" | null}      bắt đầu lượt nói; topic null = tự đoán (two_pass)
  <binary>                                      PCM int16 little-endian, 16kHz, mono, ~100ms/frame
  {"type": "stop"}                              kết thúc lượt nói, server bắt đầu nhận dạng
  {"type": "cancel"}                            huỷ lượt hiện tại (kể cả khi đang nhận dạng)
  {"type": "correction", "turn_id": "...", "text": "..."}   người dùng sửa tay transcript
  {"type": "speak", "text": "..."}              đọc text (thay lượt đọc đang chạy)
  {"type": "speak_stop"}                        dừng đọc

Server → Client
  {"type": "hello", "topics": [...], "model": "...", "sample_rate": 16000,
   "tts": {"sample_rate": 48000, "voice": "..."} | null}          null = server tắt TTS
  {"type": "state", "value": "IDLE" | "LISTENING" | "TRANSCRIBING"}
  {"type": "transcript", "turn_id", "text", "topic", "topic_auto", "audio_ms", "stt_ms"}
  {"type": "error", "code": "...", "message": "..."}
  {"type": "speak_start", "speak_id": n, "sentences": [{"text", "source", "lang"}, ...]}
  {"type": "speak_sentence", "speak_id": n, "index": i}          câu i bắt đầu (gửi trước audio của câu)
  <binary>  4 byte uint32 LE speak_id + PCM int16 LE mono, sample rate trong hello.tts
  {"type": "speak_end", "speak_id": n, "cancelled": bool, "ttfa_ms", "synth_ms", "audio_ms"}

speak_id tăng dần trong một kết nối; client bỏ audio có speak_id cũ (đến trễ sau khi bấm dừng).
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
    TTS_FAILED = "tts_failed"
    TTS_DISABLED = "tts_disabled"


def hello(topics: list[str], model: str, tts: dict | None = None) -> dict:
    return {"type": "hello", "topics": topics, "model": model, "sample_rate": SAMPLE_RATE, "tts": tts}


def state(value: State) -> dict:
    return {"type": "state", "value": str(value)}


def transcript(turn_id: str, text: str, topic: str | None, topic_auto: bool,
               audio_ms: int, stt_ms: int) -> dict:
    return {"type": "transcript", "turn_id": turn_id, "text": text, "topic": topic,
            "topic_auto": topic_auto, "audio_ms": audio_ms, "stt_ms": stt_ms}


def error(code: ErrorCode, message: str) -> dict:
    return {"type": "error", "code": str(code), "message": message}


def speak_start(speak_id: int, sentences: list[dict]) -> dict:
    return {"type": "speak_start", "speak_id": speak_id, "sentences": sentences}


def speak_sentence(speak_id: int, index: int) -> dict:
    return {"type": "speak_sentence", "speak_id": speak_id, "index": index}


def speak_end(speak_id: int, cancelled: bool, ttfa_ms: int | None, synth_ms: int, audio_ms: int) -> dict:
    return {"type": "speak_end", "speak_id": speak_id, "cancelled": cancelled,
            "ttfa_ms": ttfa_ms, "synth_ms": synth_ms, "audio_ms": audio_ms}


def audio_frame(speak_id: int, pcm16: bytes) -> bytes:
    return speak_id.to_bytes(4, "little") + pcm16
