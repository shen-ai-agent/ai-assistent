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
  Giao việc cho VS Code (T-011, chỉ khi hello.agent khác null):
  {"type": "projects"}                          xin danh sách dự án
  {"type": "task", "text": "...", "project": "<name>" | null}
        project null = server tách tên dự án từ text ("mở dự án job hay rồi ..."); có project thì cả text là
        yêu cầu, text rỗng = chỉ mở dự án
  {"type": "task_cancel", "task_id": n}         ngừng theo dõi (Claude trong VS Code vẫn chạy tiếp)
  {"type": "task_open", "task_id": n}           mở lại phiên Claude của task trong VS Code
  {"type": "task_fix", "task_id": n, "project": "<name>", "request": "..." | null}   giao nhầm dự án: thôi theo
        dõi task cũ, giao lại ở project (request null = giữ yêu cầu cũ), ghi nhãn _local/logs/intents-*.jsonl
  {"type": "work", "on": bool, "project": "<name>" | null}   chế độ công việc (tab Công việc đang mở): mọi câu
        nói/gõ hỏi Mimi đều là việc cho VS Code; câu không nêu tên dự án thì làm ở project (null = dự án của
        việc gần nhất); "tiến độ sao rồi" thì đọc trạng thái
  Tab Học (T-012, chỉ khi hello.study khác null; thêm/xoá tài liệu qua REST /api/study):
  {"type": "study", "doc_id": "<id>" | null}    tab Học đang mở tài liệu doc_id (null = rời tab / đóng tài liệu):
        mọi câu nói/gõ hỏi Mimi (trừ lệnh "dừng", "đọc lại") đi tới gia sư Claude riêng của tài liệu đó

Server → Client
  {"type": "hello", "topics": [...], "model": "...", "sample_rate": 16000,
   "tts": {"sample_rate": 48000, "voice": "..."} | null,          null = server tắt TTS
   "bridge": {"kind": "claude_cli" | "clipboard", "auto_ask": bool, "session_id",
              "model": str | null, "models": [...]} | null,     model null = bridge không chọn được model
   "assistant": {"name": "Mimi", "sleep_silence_ms", "active_silence_ms"},   im lặng server chờ để hết đoạn
   "study": {"max_chars"} | null}               null = tab Học tắt (bridge không phải claude_cli)
  {"type": "state", "value": "IDLE" | "LISTENING" | "TRANSCRIBING" | "WAITING_CLAUDE"}
  {"type": "transcript", "turn_id", "text", "topic", "topic_auto", "audio_ms", "stt_ms"}
  {"type": "error", "code": "...", "message": "..."}
  {"type": "speak_start", "speak_id": n}
  {"type": "speak_more", "speak_id": n, "sentences": [{"text", "source", "lang"}, ...]}   thêm câu vào cuối
  {"type": "speak_sentence", "speak_id": n, "index": i}          câu i (đếm từ đầu lượt) bắt đầu, trước audio của câu
  <binary>  4 byte uint32 LE speak_id + PCM int16 LE mono, sample rate trong hello.tts
  {"type": "speak_end", "speak_id": n, "cancelled": bool, "ttfa_ms", "synth_ms", "audio_ms"}

  {"type": "answer_start", "ask_id": n, "question": "...", "speak_id": n | null, "doc_id": str | null}
        câu trả lời đọc bằng speak_id này; doc_id khác null = lượt giảng của tài liệu đó (tab Học),
        session_id của answer_end khi đó là session của tài liệu, không phải của tab Mimi
  {"type": "answer_delta", "ask_id": n, "text": "..."}          text thô (markdown, có thể có ---CODE---)
  {"type": "answer_end", "ask_id": n, "cancelled": bool, "error": str | null, "session_id",
   "first_text_ms", "total_ms", "cost_usd", "model"}            model: tên model thật đã trả lời
  {"type": "bridge_session", "session_id": str | null}           sau new_session
  {"type": "bridge_model", "model": str}                         sau set_model, gửi tới mọi tab
  {"type": "study_doc", "doc": {"id", "title", "source", "chars", "created", "updated", "started", "part", "total",
   "parts": {"k": "tên phần"}}}                                 tiến độ tài liệu đổi (đầu lượt giảng, cuối lượt)
  {"type": "command", "name": "new_session" | "stop" | "repeat", "text", "reply": str | null}
        câu hỏi ngắn là lệnh (server/assistant/commands.py): chạy ngay, không hỏi Claude; reply được đọc

  Rảnh tay (T-010):
  {"type": "listen", "mode": "OFF" | "SLEEP" | "ACTIVE" | "BUSY"}
        SLEEP chờ gọi tên; ACTIVE đoạn nói kế tiếp là câu hỏi; BUSY đang nghĩ/đọc, chỉ nghe tên để ngắt lời
  {"type": "vad", "speaking": bool}                              VAD thấy bắt đầu / hết tiếng nói
  {"type": "heard", "text", "wake": bool, "action": "ack" | "ask" | "ignore", "ack": str | null, "stt_ms"}
        một đoạn nói đã nhận dạng để tìm tên. ack = câu đáp sẽ đọc; ask = có câu hỏi ngay sau tên.
        Câu hỏi (sau ack hoặc ask) đến bằng transcript như push-to-talk.

  Giao việc cho VS Code (T-011). hello có thêm "agent": {"auto_submit": bool} | null. Các message task_*
  gửi tới MỌI tab; câu giao việc nói/gõ ở tab Mimi trả command name "dev_task" (reply là câu đáp) trước task_start.
  Câu hỏi về việc ("tiến độ sao rồi", "ngừng theo dõi") hoặc Mimi hỏi lại tên dự án: command name "work"
  (reply là câu đọc), không có task_start. Câu tiếp theo trả lời tên dự án ("job hay", "ừ", "cái thứ hai",
  "không") thì giao việc đang chờ. Khi hỏi lại có ứng viên, command có thêm "choices": ["<name>", ...] để web
  hiện nút chọn (bấm = gửi ask với tên đó). Nói "sai rồi" ngay sau khi giao việc: thôi theo dõi việc đó, hỏi lại.
  {"type": "projects", "items": [{"name", "path", "description", "stack", "last_used"}]}   dùng gần nhất trước
  {"type": "tasks", "items": [<task>]}          ảnh chụp mọi task, gửi sau hello. <task> = {"task_id", "project":
        {"name","path"} | null, "request", "source": "voice"|"text", "status", "detail", "steps": [{"step_id",
        "tool", "summary", "ok"}], "report", "files", "tools", "duration_ms", "session_id", "started_at"}
  {"type": "task_start", "task_id", "project", "request", "source", "started_at"}
  {"type": "task_status", "task_id", "status": "opening" | "prefilled" | "running" | "waiting", "detail"}
        prefilled = yêu cầu đã điền sẵn, chờ bấm Enter trong VS Code; waiting = một tool chưa xong lâu
        (Claude chờ duyệt trong VS Code hoặc lệnh chạy lâu, log không phân biệt được)
  {"type": "task_step", "task_id", "step_id", "tool", "summary"}
  {"type": "task_step_end", "task_id", "step_id", "ok": bool}
  {"type": "task_end", "task_id", "status": "done" | "failed" | "cancelled" | "interrupted", "report",
   "files", "tools", "duration_ms", "session_id"}        report = câu trả lời cuối của Claude hoặc lời giải thích lỗi

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
    AGENT_DISABLED = "agent_disabled"
    STUDY_DISABLED = "study_disabled"


def hello(topics: list[str], model: str, tts: dict | None = None, bridge: dict | None = None,
          assistant: dict | None = None, agent: dict | None = None, study: dict | None = None) -> dict:
    return {"type": "hello", "topics": topics, "model": model, "sample_rate": SAMPLE_RATE, "tts": tts,
            "bridge": bridge, "assistant": assistant, "agent": agent, "study": study}


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


def answer_start(ask_id: int, question: str, speak_id: int | None, doc_id: str | None = None) -> dict:
    return {"type": "answer_start", "ask_id": ask_id, "question": question, "speak_id": speak_id, "doc_id": doc_id}


def answer_delta(ask_id: int, text: str) -> dict:
    return {"type": "answer_delta", "ask_id": ask_id, "text": text}


def answer_end(ask_id: int, cancelled: bool, error: str | None, session_id: str | None,
               first_text_ms: int | None, total_ms: int, cost_usd: float | None,
               model: str | None = None) -> dict:
    return {"type": "answer_end", "ask_id": ask_id, "cancelled": cancelled, "error": error,
            "session_id": session_id, "first_text_ms": first_text_ms, "total_ms": total_ms, "cost_usd": cost_usd,
            "model": model}


def command(name: str, text: str, reply: str | None, choices: list[str] | None = None) -> dict:
    msg = {"type": "command", "name": str(name), "text": text, "reply": reply}
    if choices:
        msg["choices"] = choices
    return msg


def bridge_session(session_id: str | None) -> dict:
    return {"type": "bridge_session", "session_id": session_id}


def bridge_model(model: str) -> dict:
    return {"type": "bridge_model", "model": model}


def study_doc(doc: dict) -> dict:
    return {"type": "study_doc", "doc": doc}


def listen(mode: ListenMode) -> dict:
    return {"type": "listen", "mode": str(mode)}


def vad(speaking: bool) -> dict:
    return {"type": "vad", "speaking": speaking}


def heard(text: str, wake: bool, action: str, ack: str | None, stt_ms: int) -> dict:
    return {"type": "heard", "text": text, "wake": wake, "action": action, "ack": ack, "stt_ms": stt_ms}


def projects(items: list[dict]) -> dict:
    return {"type": "projects", "items": items}


def tasks(items: list[dict]) -> dict:
    return {"type": "tasks", "items": items}


def task_start(task_id: int, project: dict | None, request: str, source: str, started_at: float) -> dict:
    return {"type": "task_start", "task_id": task_id, "project": project, "request": request, "source": source,
            "started_at": started_at}


def task_status(task_id: int, status: str, detail: str | None = None) -> dict:
    return {"type": "task_status", "task_id": task_id, "status": status, "detail": detail}


def task_step(task_id: int, step_id: int, tool: str, summary: str) -> dict:
    return {"type": "task_step", "task_id": task_id, "step_id": step_id, "tool": tool, "summary": summary}


def task_step_end(task_id: int, step_id: int, ok: bool) -> dict:
    return {"type": "task_step_end", "task_id": task_id, "step_id": step_id, "ok": ok}


def task_end(task_id: int, status: str, report: str, files: list[str], tools: int, duration_ms: int,
             session_id: str | None) -> dict:
    return {"type": "task_end", "task_id": task_id, "status": status, "report": report, "files": files,
            "tools": tools, "duration_ms": duration_ms, "session_id": session_id}
