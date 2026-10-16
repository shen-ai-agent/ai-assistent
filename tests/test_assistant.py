"""Test trợ lý rảnh tay không cần GPU: so khớp tên, cắt đoạn nói với VAD giả."""

from __future__ import annotations

import numpy as np
import pytest

from server.assistant import Segmenter, StreamingVad, WakeMatcher
from server.assistant.vad import WINDOW


class FakeVadSession:
    """Xác suất = 1 khi cửa sổ có biên độ lớn, 0 khi im lặng. Ghi lại context để kiểm tra cách ghép."""

    def __init__(self):
        self.inputs: list[np.ndarray] = []

    def run(self, _, feeds):
        x = feeds["input"]
        self.inputs.append(x)
        p = (np.abs(x[:, 64:]).mean(axis=1, keepdims=True) > 0.1).astype(np.float32)
        return p, feeds["h"], feeds["c"]


def tone(ms: int) -> np.ndarray:
    return (np.sin(np.arange(ms * 16) / 5) * 0.5).astype(np.float32)


def silence(ms: int) -> np.ndarray:
    return np.zeros(ms * 16, dtype=np.float32)


@pytest.fixture
def matcher():
    return WakeMatcher("Mimi", ["mi mi", "memi"])


@pytest.mark.parametrize("text, rest", [
    ("Mimi ơi", ""),
    ("Hey Mimi.", ""),
    ("Ê Mimi, EC2 là gì?", "EC2 là gì?"),
    ("Mi Mi ơi!", ""),
    ("Mimmy ơi", ""),
    ("Mimì à", ""),
    ("Này Mimi ơi, cho mình hỏi S3 là gì", "cho mình hỏi S3 là gì"),
    ("Alo Mimi nè", ""),
    ("hey memi what is a VPC", "what is a VPC"),
])
def test_wake_found(matcher, text, rest):
    m = matcher.match(text)
    assert m.found and m.rest == rest


@pytest.mark.parametrize("text", [
    "Hôm qua Mimi nói với mình",     # tên giữa câu: đang kể chuyện, không phải gọi
    "Minh ơi",
    "Mini PC này chạy được không",
    "Em ơi cho chị hỏi",
    "Ê, mai mấy giờ họp?",           # "mai" + "mấy" ghép thành "maimai": dài hơn tên, không được tính
    "Mimosa là một loài hoa",
    "Cảm ơn các bạn đã theo dõi",
    "",
])
def test_wake_not_found(matcher, text):
    assert not matcher.match(text).found


def test_wake_rest_words(matcher):
    assert matcher.match("Mimi ơi EC2 là gì").rest_words == 3
    assert matcher.match("Mimi ơi").rest_words == 0


def test_streaming_vad_carries_context():
    sess = FakeVadSession()
    vad = StreamingVad(sess)
    a = np.arange(WINDOW * 3, dtype=np.float32)
    assert len(vad.push(a[:700])) == 1          # 700 mẫu = 1 cửa sổ + 188 mẫu chờ
    assert len(vad.push(a[700:])) == 2
    x = np.vstack(sess.inputs)
    assert x.shape == (3, 576)
    assert (x[0, :64] == 0).all()
    assert (x[1, :64] == a[WINDOW - 64:WINDOW]).all()   # context = 64 mẫu cuối của cửa sổ trước
    assert (x[2, 64:] == a[2 * WINDOW:]).all()


def feed(seg: Segmenter, audio: np.ndarray, frame: int = 1600) -> list[tuple]:
    return [e for i in range(0, len(audio), frame) for e in seg.push(audio[i:i + frame])]


def test_segmenter_cuts_on_silence():
    seg = Segmenter(StreamingVad(FakeVadSession()), silence_ms=500, preroll_ms=96, tail_ms=0, min_speech_ms=200)
    events = feed(seg, np.concatenate([silence(1000), tone(800), silence(300), tone(400), silence(700)]))
    kinds = [e[0] for e in events]
    assert kinds == ["start", "end"]   # khoảng lặng 300ms < 500ms: vẫn một đoạn
    s = events[1][1]
    assert abs(s.start / 16 - (1000 - 96)) <= 32       # có preroll trước tiếng nói
    assert abs(s.audio_ms - (96 + 800 + 300 + 400)) <= 64
    assert not seg.in_speech


def test_segmenter_drops_short_noise_and_forces_long():
    seg = Segmenter(StreamingVad(FakeVadSession()), silence_ms=300, min_speech_ms=200, max_s=1, preroll_ms=0)
    events = feed(seg, np.concatenate([tone(100), silence(500), tone(2500), silence(500)]))
    assert [e[0] for e in events][:2] == ["start", "drop"]
    ends = [e[1] for e in events if e[0] == "end"]
    assert ends[0].forced and ends[0].audio_ms == pytest.approx(1000, abs=40)
    assert not ends[-1].forced


def test_segmenter_silence_changes_midway():
    seg = Segmenter(StreamingVad(FakeVadSession()), silence_ms=500)
    feed(seg, tone(600))
    seg.silence_ms = 900   # chuyển sang nghe câu hỏi: chờ im lặng lâu hơn
    assert [e[0] for e in feed(seg, silence(700))] == []
    assert [e[0] for e in feed(seg, silence(300))] == ["end"]


@pytest.mark.parametrize("text, cmd", [
    ("Không, mở session mới đi.", "new_session"),
    ("Mở phiên mới", "new_session"),
    ("New chat please", "new_session"),
    ("Quên hết đi, nói chuyện khác", "new_session"),
    ("Tạo section chat mới cho mình.", "new_session"),   # Whisper viết session thành section
    ("Mở cuộc trò chuyện mới", "new_session"),
    ("Chuyển sang chủ đề mới nhé", "new_session"),
    ("Session mới đi", "new_session"),
    ("Làm sao tạo session mới trong ASP.NET Core?", None),
    ("Tính năng chat mới của Teams có gì hay?", None),
    ("Cách mở phiên mới trong Redis", None),
    ("Dừng lại.", "stop"),
    ("Thôi được rồi", "stop"),
    ("Stop", "stop"),
    ("Đọc lại đi", "repeat"),
    ("Say again?", "repeat"),
    ("Đọc lại câu vừa rồi chậm hơn được không", None),   # câu dài: hỏi Claude
    ("Làm sao dừng một EC2 instance?", None),
    ("Session trong ASP.NET Core lưu ở đâu khi scale ra nhiều server?", None),
    ("Thôi, giải thích lại S3 đi", None),
    ("", None),
])
def test_match_command(text, cmd):
    from server.assistant.commands import match_command
    assert match_command(text) == cmd


# ---------- Rảnh tay qua WebSocket: VAD giả (biên độ), engine theo kịch bản ----------

import json  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from server.app import create_app  # noqa: E402
from server.config import Config  # noqa: E402
from server.stt.engine import Transcript  # noqa: E402
from test_server import FakeBridge, FakeTts, recv_any  # noqa: E402


class ScriptedEngine:
    """Lượt không prompt (tìm tên) lấy lần lượt từ wake; lượt có prompt (câu hỏi) lấy từ questions."""

    def __init__(self, wake: list[str], questions: list[str] | None = None):
        self.wake, self.questions = list(wake), list(questions or [])
        self.plain_calls = 0

    def transcribe(self, audio, *, initial_prompt=None, **_):
        if not np.abs(audio).max():
            return Transcript("", "vi", 1.0)  # warm-up
        if initial_prompt is None:
            self.plain_calls += 1
            return Transcript(self.wake.pop(0), "vi", 1.0)
        return Transcript(self.questions.pop(0), "vi", 1.0)


def hf_client(tmp_path, engine, bridge=None, tts=None, **cfg):
    cfg = Config(log_dir=tmp_path / "logs", save_audio=True, min_speech_ms=0, tts_enabled=tts is not None,
                 bridge_log_dir=tmp_path / "sessions", assistant_follow_up_s=1, assistant_active_timeout_s=2,
                 **cfg)
    return TestClient(create_app(cfg, engine_factory=lambda _cfg: engine, tts_factory=lambda _cfg: tts,
                                 bridge_factory=lambda _cfg: bridge, vad_factory=FakeVadSession))


def pcm16(audio: np.ndarray) -> list[bytes]:
    data = (audio * 32767).astype("<i2").tobytes()
    return [data[i:i + 3200] for i in range(0, len(data), 3200)]


def say(ws, ms: int, pause_ms: int = 1000):
    for frame in pcm16(np.concatenate([tone(ms), silence(pause_ms)])):
        ws.send_bytes(frame)


def until(ws, pred, keep=None):
    """Đọc message (bỏ audio) tới khi pred(msg) đúng. keep: danh sách để gom các message đã đọc."""
    while True:
        msg = recv_any(ws)
        if isinstance(msg, dict):
            if keep is not None:
                keep.append(msg)
            if pred(msg):
                return msg


def mode(m):
    return lambda msg: msg["type"] == "listen" and msg["mode"] == m


def start_listen(ws):
    hello = ws.receive_json()
    assert hello["assistant"]["name"] == "Mimi"
    ws.send_json({"type": "listen", "on": True, "topic": "AWS"})
    until(ws, mode("SLEEP"))


def test_handsfree_wake_ack_question_follow_up(tmp_path):
    engine = ScriptedEngine(["Mimi ơi"], ["EC2 là gì?", "Còn S3?"])
    bridge = FakeBridge(["EC2 là máy ảo."])
    with hf_client(tmp_path, engine, bridge, FakeTts()) as client, client.websocket_connect("/ws") as ws:
        start_listen(ws)
        say(ws, 600)
        heard = until(ws, lambda m: m["type"] == "heard")
        assert heard["wake"] and heard["action"] == "ack" and heard["ack"] in Config().assistant_acks
        assert until(ws, lambda m: m["type"] == "speak_end")["cancelled"] is False
        say(ws, 0, 1500)                  # chờ loa phát xong câu đáp
        until(ws, mode("ACTIVE"))
        say(ws, 800, 1200)                # câu hỏi, không cần gọi tên
        msgs = []
        until(ws, lambda m: m["type"] == "answer_end", msgs)
        kinds = [m["type"] for m in msgs]
        assert kinds.index("transcript") < kinds.index("answer_start")
        assert {"type": "listen", "mode": "BUSY"} in msgs
        say(ws, 0, 1500)
        until(ws, mode("ACTIVE"))         # hỏi tiếp trong follow_up_s
        say(ws, 800, 1200)
        until(ws, lambda m: m["type"] == "answer_end")
        say(ws, 0, 3000)
        until(ws, mode("SLEEP"))          # hết follow_up_s mà im lặng
        assert bridge.questions == ["EC2 là gì?", "Còn S3?"]
        assert engine.plain_calls == 1    # câu hỏi và hỏi tiếp không phải tìm tên
    wake_log = (tmp_path / "logs").glob("wake-*.jsonl")
    rec = json.loads(next(wake_log).read_text(encoding="utf-8").splitlines()[0])
    assert rec["found"] and rec["action"] == "ack" and rec["audio_file"].startswith("wake-audio/")


def test_handsfree_name_and_question_in_one_breath(tmp_path):
    engine = ScriptedEngine(["Mimi ơi, EC2 là gì"], ["Mimi ơi, EC2 là gì?"])
    bridge = FakeBridge(["Máy ảo."])
    with hf_client(tmp_path, engine, bridge, FakeTts()) as client, client.websocket_connect("/ws") as ws:
        start_listen(ws)
        say(ws, 1500)
        heard = until(ws, lambda m: m["type"] == "heard")
        assert heard["action"] == "ask" and heard["ack"] is None
        until(ws, lambda m: m["type"] == "answer_end")
        assert bridge.questions == ["EC2 là gì?"]   # bỏ tên khỏi câu hỏi


def test_handsfree_pause_after_name_is_still_question(tmp_path):
    engine = ScriptedEngine(["Mimi ơi"], ["EC2 là gì?"])
    bridge = FakeBridge(["Máy ảo."])
    with hf_client(tmp_path, engine, bridge, FakeTts()) as client, client.websocket_connect("/ws") as ws:
        start_listen(ws)
        # ngập ngừng 600ms > sleep_silence_ms: VAD cắt hai đoạn, nhưng câu hỏi tới trước khi trợ lý kịp đáp
        for frame in pcm16(np.concatenate([tone(600), silence(600), tone(800), silence(1200)])):
            ws.send_bytes(frame)
        msgs = []
        until(ws, lambda m: m["type"] == "answer_end", msgs)
        kinds = [m["type"] for m in msgs]
        assert kinds.index("transcript") < kinds.index("answer_start") < kinds.index("speak_start")
        assert bridge.questions == ["EC2 là gì?"]
        assert engine.plain_calls == 1


def test_handsfree_ignores_speech_without_name(tmp_path):
    engine = ScriptedEngine(["Hôm qua Mimi nói với mình", "Trời hôm nay đẹp"])
    bridge = FakeBridge(["x"])
    with hf_client(tmp_path, engine, bridge) as client, client.websocket_connect("/ws") as ws:
        start_listen(ws)
        say(ws, 600)
        assert until(ws, lambda m: m["type"] == "heard")["action"] == "ignore"
        say(ws, 600)
        assert until(ws, lambda m: m["type"] == "heard")["action"] == "ignore"
        ws.send_json({"type": "listen", "on": False})
        until(ws, mode("OFF"))
        say(ws, 600)                  # tắt rảnh tay: không còn tìm tên
        ws.send_json({"type": "set_model", "model": "x"})   # message bất kỳ để chắc server đã xử lý frame
        assert until(ws, lambda m: m["type"] in ("heard", "error"))["type"] == "error"
        assert bridge.questions == []
    rec = json.loads(next((tmp_path / "logs").glob("wake-*.jsonl")).read_text(encoding="utf-8").splitlines()[0])
    assert not rec["found"] and "audio_file" not in rec


def test_handsfree_no_wake_while_speaking(tmp_path):
    """Đang đọc thì không nhận dạng đoạn nói (giữ GPU cho TTS); câu đáp đọc xong như bình thường."""
    gate = threading.Event()
    engine = ScriptedEngine(["Mimi ơi EC2 là gì"], ["EC2 là gì"])
    bridge = FakeBridge(["Câu một. ", "Câu hai."], gate=gate)
    with hf_client(tmp_path, engine, bridge, FakeTts()) as client, client.websocket_connect("/ws") as ws:
        start_listen(ws)
        say(ws, 1500)
        until(ws, lambda m: m["type"] == "answer_start")
        calls = engine.plain_calls
        say(ws, 600)                  # nói khi Claude đang trả lời: không gọi STT tìm tên
        time.sleep(0.5)               # cho server xử lý đoạn đó (không có tin nhắn nào để chờ)
        assert engine.plain_calls == calls
        gate.set()
        assert bridge.closed.wait(5)
        until(ws, lambda m: m["type"] == "speak_end" and not m["cancelled"])   # câu đáp
        say(ws, 0, 1500)
        until(ws, mode("ACTIVE"))     # câu đáp đọc xong như lần gọi bình thường


def test_handsfree_push_to_talk_still_works(tmp_path):
    engine = ScriptedEngine([], ["EC2 là gì"])
    with hf_client(tmp_path, engine) as client, client.websocket_connect("/ws") as ws:
        start_listen(ws)
        ws.send_json({"type": "start", "topic": "AWS"})
        for frame in pcm16(tone(500)):
            ws.send_bytes(frame)
        ws.send_json({"type": "stop"})
        assert until(ws, lambda m: m["type"] == "transcript")["text"] == "EC2 là gì"
        assert engine.plain_calls == 0  # frame lúc giữ Space không đi vào bộ tìm tên
