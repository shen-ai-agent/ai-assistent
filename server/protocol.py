"""Protocol WebSocket giữa web và server. Mọi message định nghĩa ở đây.

Client → Server
  {"type": "start", "topic": "AWS" | null}      bắt đầu lượt nói; topic null = tự đoán (two_pass)
  <binary>                                      PCM int16 little-endian, 16kHz, mono, ~100ms/frame
  {"type": "stop"}                              kết thúc lượt nói, server bắt đầu nhận dạng
  {"type": "cancel"}                            huỷ lượt hiện tại (kể cả khi đang nhận dạng)
  {"type": "correction", "turn_id": "...", "text": "..."}   người dùng sửa tay transcript
  {"type": "speak", "text": "..."}              đọc text (thay lượt đọc đang chạy)
  {"type": "speak_stop"}                        dừng đọc (Claude vẫn trả lời tiếp, chỉ không đọc)
  {"type": "ask", "text": "..."}                hỏi Claude bằng text gõ tay (thay lượt hỏi đang chạy)
  {"type": "ask_stop"}                          dừng hỏi và dừng đọc câu trả lời
  {"type": "new_session"}                       Claude quên ngữ cảnh cũ
  {"type": "set_model", "model": "sonnet"}      đổi model cho các lượt sau ("" = mặc định), áp cho mọi tab
  {"type": "listen", "on": bool, "topic": "AWS" | null}   bật/tắt rảnh tay (gửi lại để đổi chủ đề).
                                                Khi bật, client gửi frame PCM liên tục; giữ Space vẫn dùng được

Server → Client
  {"type": "hello", "topics": [...], "model": "...", "sample_rate": 16000,
   "tts": {"sample_rate": 48000, "voice": "..."} | null,          null = server tắt TTS
   "bridge": {"kind": "claude_cli" | "clipboard", "auto_ask": bool, "session_id",
              "model": str | null, "models": [...]} | null,     model null = bridge không chọn được model
   "assistant": {"name": "Mimi", "sleep_silence_ms", "active_silence_ms"}}   im lặng server chờ để hết đoạn
  {"type": "state", "value": "IDLE" | "LISTENING" | "TRANSCRIBING" | "WAITING_CLAUDE"}
  {"type": "transcript", "turn_id", "text", "topic", "topic_auto", "audio_ms", "stt_ms"}
  {"type": "error", "code": "...", "message": "..."}
  {"type": "speak_start", "speak_id": n}
  {"type": "speak_more", "speak_id": n, "sentences": [{"text", "source", "lang"}, ...]}   thêm câu vào cuối
  {"type": "speak_sentence", "speak_id": n, "index": i}          câu i (đếm từ đầu lượt) bắt đầu, trước audio của câu
  <binary>  4 byte uint32 LE speak_id + PCM int16 LE mono, sample rate trong hello.tts
  {"type": "speak_end", "speak_id": n, "cancelled": bool, "ttfa_ms", "synth_ms", "audio_ms"}

  {"type": "answer_start", "ask_id": n, "question": "...", "speak_id": n | null}   câu trả lời đọc bằng speak_id này
  {"type": "answer_delta", "ask_id": n, "text": "..."}          text thô (markdown, có thể có ---CODE---)
  {"type": "answer_end", "ask_id": n, "cancelled": bool, "error": str | null, "session_id",
   "first_text_ms", "total_ms", "cost_usd", "model"}            model: tên model thật đã trả lời
  {"type": "bridge_session", "session_id": str | null}           sau new_session
  {"type": "bridge_model", "model": str}                         sau set_model, gửi tới mọi tab
  {"type": "command", "name": "new_session" | "stop" | "repeat", "text", "reply": str | null}
        câu hỏi ngắn là lệnh (server/assistant/commands.py): chạy ngay, không hỏi Claude; reply được đọc

  Rảnh tay (T-010):
  {"type": "listen", "mode": "OFF" | "SLEEP" | "ACTIVE" | "BUSY"}
        SLEEP chờ gọi tên; ACTIVE đoạn nói kế tiếp là câu hỏi; BUSY đang nghĩ/đọc, chỉ nghe tên để ngắt lời
  {"type": "vad", "speaking": bool}                              VAD thấy bắt đầu / hết tiếng nói
  {"type": "heard", "text", "wake": bool, "action": "ack" | "ask" | "ignore", "ack": str | null, "stt_ms"}
        một đoạn nói đã nhận dạng để tìm tên. ack = câu đáp sẽ đọc; ask = có câu hỏi ngay sau tên.
        Câu hỏi (sau ack hoặc ask) đến bằng transcript như push-to-talk.

speak_id tăng dần trong một kết nối; client bỏ audio có speak_id cũ (đến trễ sau khi bấm dừng).
Câu trả lời của Claude đến dần nên câu được gửi dần bằng speak_more; speak_end sau câu cuối.
"""

from __future__ import annotations

from enum import StrEnum

SAMPLE_RATE = 16000


class State(StrEnum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    TRANSCRIBING = "TRANSCRIBING"
    WAITING_CLAUDE = "WAITING_CLAUDE"


class ListenMode(StrEnum):
    OFF = "OFF"
    SLEEP = "SLEEP"
    ACTIVE = "ACTIVE"
    BUSY = "BUSY"


class ErrorCode(StrEnum):
    BAD_MESSAGE = "bad_message"
    BAD_TOPIC = "bad_topic"
    TOO_SHORT = "too_short"
    NO_SPEECH = "no_speech"
    STT_FAILED = "stt_failed"
    TTS_FAILED = "tts_failed"
    TTS_DISABLED = "tts_disabled"
    BRIDGE_DISABLED = "bridge_disabled"
    BRIDGE_FAILED = "bridge_failed"


def hello(topics: list[str], model: str, tts: dict | None = None, bridge: dict | None = None,
          assistant: dict | None = None) -> dict:
    return {"type": "hello", "topics": topics, "model": model, "sample_rate": SAMPLE_RATE, "tts": tts,
            "bridge": bridge, "assistant": assistant}


def state(value: State) -> dict:
    return {"type": "state", "value": str(value)}


def transcript(turn_id: str, text: str, topic: str | None, topic_auto: bool,
               audio_ms: int, stt_ms: int) -> dict:
    return {"type": "transcript", "turn_id": turn_id, "text": text, "topic": topic,
            "topic_auto": topic_auto, "audio_ms": audio_ms, "stt_ms": stt_ms}


def error(code: ErrorCode, message: str) -> dict:
    return {"type": "error", "code": str(code), "message": message}


def speak_start(speak_id: int) -> dict:
    return {"type": "speak_start", "speak_id": speak_id}


def speak_more(speak_id: int, sentences: list[dict]) -> dict:
    return {"type": "speak_more", "speak_id": speak_id, "sentences": sentences}


def speak_sentence(speak_id: int, index: int) -> dict:
    return {"type": "speak_sentence", "speak_id": speak_id, "index": index}


def speak_end(speak_id: int, cancelled: bool, ttfa_ms: int | None, synth_ms: int, audio_ms: int) -> dict:
    return {"type": "speak_end", "speak_id": speak_id, "cancelled": cancelled,
            "ttfa_ms": ttfa_ms, "synth_ms": synth_ms, "audio_ms": audio_ms}


def audio_frame(speak_id: int, pcm16: bytes) -> bytes:
    return speak_id.to_bytes(4, "little") + pcm16


def answer_start(ask_id: int, question: str, speak_id: int | None) -> dict:
    return {"type": "answer_start", "ask_id": ask_id, "question": question, "speak_id": speak_id}


def answer_delta(ask_id: int, text: str) -> dict:
    return {"type": "answer_delta", "ask_id": ask_id, "text": text}


def answer_end(ask_id: int, cancelled: bool, error: str | None, session_id: str | None,
               first_text_ms: int | None, total_ms: int, cost_usd: float | None,
               model: str | None = None) -> dict:
    return {"type": "answer_end", "ask_id": ask_id, "cancelled": cancelled, "error": error,
            "session_id": session_id, "first_text_ms": first_text_ms, "total_ms": total_ms, "cost_usd": cost_usd,
            "model": model}


def command(name: str, text: str, reply: str | None) -> dict:
    return {"type": "command", "name": str(name), "text": text, "reply": reply}


def bridge_session(session_id: str | None) -> dict:
    return {"type": "bridge_session", "session_id": session_id}


def bridge_model(model: str) -> dict:
    return {"type": "bridge_model", "model": model}


def listen(mode: ListenMode) -> dict:
    return {"type": "listen", "mode": str(mode)}


def vad(speaking: bool) -> dict:
    return {"type": "vad", "speaking": speaking}


def heard(text: str, wake: bool, action: str, ack: str | None, stt_ms: int) -> dict:
    return {"type": "heard", "text": text, "wake": wake, "action": action, "ack": ack, "stt_ms": stt_ms}
