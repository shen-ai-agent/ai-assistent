"""Test vòng WebSocket với engine giả: không cần GPU, chạy vài giây."""

from __future__ import annotations

import asyncio
import json
import threading

import numpy as np
import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.bridge import BridgeError, Done, TextDelta
from server.config import Config
from server.stt.engine import Transcript


class FakeEngine:
    def __init__(self):
        self.calls: list[dict] = []

    def transcribe(self, audio, *, language="vi", initial_prompt=None, hotwords=None, beam_size=5, **_):
        self.calls.append({"n": len(audio), "prompt": initial_prompt})
        return Transcript("mình muốn hỏi về EC2 và S3", "vi", 1.0)


@pytest.fixture
def setup(tmp_path):
    engine = FakeEngine()
    cfg = Config(log_dir=tmp_path / "logs", save_audio=True, min_speech_ms=0)  # audio test là sóng sin, VAD sẽ loại
    app = create_app(cfg, engine_factory=lambda _cfg: engine)
    with TestClient(app) as client:
        yield client, engine, cfg


def pcm(seconds: float) -> bytes:
    return (np.sin(np.arange(int(16000 * seconds)) / 10) * 8000).astype("<i2").tobytes()


def recv_until(ws, kind):
    while True:
        msg = ws.receive_json()
        if msg["type"] == kind:
            return msg


def test_health(setup):
    client, _, _ = setup
    body = client.get("/health").json()
    assert body["status"] == "ok" and "AWS" in body["topics"]


def test_pwa_files(setup):
    client, _, _ = setup
    manifest = client.get("/manifest.json").json()
    assert manifest["display"] == "standalone"
    for icon in manifest["icons"]:
        assert client.get("/" + icon["src"]).status_code == 200
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
    assert client.get("/offline.html").status_code == 200


def test_lan_cert(tmp_path):
    cfg = Config(log_dir=tmp_path / "logs", lan_cert=tmp_path / "lan.crt")
    with TestClient(create_app(cfg, engine_factory=lambda _cfg: FakeEngine())) as client:
        assert client.get("/lan.crt").status_code == 404
        (tmp_path / "lan.crt").write_text("-----BEGIN CERTIFICATE-----\n")
        resp = client.get("/lan.crt")
        assert resp.status_code == 200 and resp.headers["content-type"] == "application/x-x509-ca-cert"


def test_turn_with_topic(setup):
    client, engine, cfg = setup
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and "AWS" in hello["topics"]
        ws.send_json({"type": "start", "topic": "AWS"})
        assert recv_until(ws, "state")["value"] in ("IDLE", "LISTENING")
        for _ in range(10):
            ws.send_bytes(pcm(0.1))
        ws.send_json({"type": "stop"})
        tr = recv_until(ws, "transcript")
        assert tr["text"] == "mình muốn hỏi về EC2 và S3"
        assert tr["topic"] == "AWS" and tr["topic_auto"] is False
        assert tr["audio_ms"] == 1000
        assert recv_until(ws, "state")["value"] == "IDLE"

        ws.send_json({"type": "correction", "turn_id": tr["turn_id"], "text": "sửa tay"})
        ws.send_json({"type": "cancel"})  # đồng bộ: đợi server xử lý xong correction
        recv_until(ws, "state")

    assert engine.calls[-1]["n"] == 16000
    logs = list((cfg.log_dir).glob("turns-*.jsonl"))
    records = [json.loads(line) for line in logs[0].read_text(encoding="utf-8").splitlines()]
    assert [r["event"] for r in records] == ["turn", "correction"]
    assert (cfg.log_dir / records[0]["audio_file"]).exists()


def test_auto_topic_runs_two_pass(setup):
    client, engine, _ = setup
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "start", "topic": None})
        ws.send_bytes(pcm(1.0))
        ws.send_json({"type": "stop"})
        tr = recv_until(ws, "transcript")
    assert tr["topic"] == "AWS" and tr["topic_auto"] is True
    assert len(engine.calls) == 1 + 2  # warm-up + 2 lượt


def test_too_short_and_bad_messages(setup):
    client, _, _ = setup
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "start", "topic": "AWS"})
        ws.send_bytes(pcm(0.1))
        ws.send_json({"type": "stop"})
        assert recv_until(ws, "error")["code"] == "too_short"
        ws.send_json({"type": "start", "topic": "Không có"})
        assert recv_until(ws, "error")["code"] == "bad_topic"
        ws.send_text("không phải json")
        assert recv_until(ws, "error")["code"] == "bad_message"


def test_audio_ignored_when_not_listening(setup):
    client, engine, _ = setup
    with client.websocket_connect("/ws") as ws:
        ws.send_bytes(pcm(1.0))  # chưa start
        ws.send_json({"type": "start", "topic": "AWS"})
        ws.send_bytes(pcm(0.5))
        ws.send_json({"type": "stop"})
        tr = recv_until(ws, "transcript")
    assert tr["audio_ms"] == 500


class FakeTts:
    """Mỗi câu 3 mẩu 0.1s. gate: chặn trước mẩu thứ hai để test dừng giữa chừng."""

    sample_rate = 48000

    def __init__(self, gate: threading.Event | None = None):
        self.gate = gate
        self.texts: list[str] = []

    def stream(self, text, lang):
        self.texts.append(text)
        for i in range(3):
            if i == 1 and self.gate is not None:
                self.gate.wait(5)
            yield np.full(4800, 0.5, dtype=np.float32)


TWO_SRC = ["Hôm nay mình học về EC2 nhé.", "Sau đó mình chuyển sang phần S3 bucket."]
TWO = " ".join(TWO_SRC)


def tts_client(tmp_path, tts):
    cfg = Config(log_dir=tmp_path / "logs", save_audio=False, min_speech_ms=0, tts_enabled=True)
    return TestClient(create_app(cfg, engine_factory=lambda _cfg: FakeEngine(), tts_factory=lambda _cfg: tts))


def recv_any(ws):
    msg = ws.receive()
    if msg.get("bytes") is not None:
        return msg["bytes"]
    return json.loads(msg["text"])


def test_speak_streams_audio(tmp_path):
    tts = FakeTts()
    with tts_client(tmp_path, tts) as client:
        assert client.get("/health").json()["tts"] is True
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["tts"] == {"sample_rate": 48000, "voice": "maichi"}
            ws.send_json({"type": "speak", "text": TWO})
            frames, sentences, planned = [], [], []
            while True:
                msg = recv_any(ws)
                if isinstance(msg, bytes):
                    frames.append(msg)
                elif msg["type"] == "speak_start":
                    start = msg
                elif msg["type"] == "speak_more":
                    planned += msg["sentences"]
                elif msg["type"] == "speak_sentence":
                    sentences.append(msg["index"])
                elif msg["type"] == "speak_end":
                    end = msg
                    break
    assert [s["source"] for s in planned] == TWO_SRC
    assert sentences == [0, 1] and len(frames) == 6
    assert all(int.from_bytes(f[:4], "little") == start["speak_id"] for f in frames)
    assert len(frames[0]) == 4 + 4800 * 2 and np.frombuffer(frames[0][4:], "<i2")[0] == 16383
    assert end["cancelled"] is False and end["audio_ms"] == 600 and end["ttfa_ms"] is not None


def test_speak_stop_cancels(tmp_path):
    gate = threading.Event()
    tts = FakeTts(gate)
    with tts_client(tmp_path, tts) as client:
        gate.set()  # warm-up chạy thẳng
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            gate.clear()
            ws.send_json({"type": "speak", "text": TWO})
            while not isinstance(recv_any(ws), bytes):
                pass  # mẩu đầu, mẩu sau bị gate chặn
            ws.send_json({"type": "speak_stop"})
            gate.set()
            while not (isinstance(end := recv_any(ws), dict) and end["type"] == "speak_end"):
                pass
    assert end["cancelled"] is True
    assert len(tts.texts) == 1 + 1  # warm-up + câu một, không đọc sang câu hai


def test_start_interrupts_speaking(tmp_path):
    gate = threading.Event()
    tts = FakeTts(gate)
    with tts_client(tmp_path, tts) as client:
        gate.set()
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            gate.clear()
            ws.send_json({"type": "speak", "text": TWO_SRC[0]})
            while not isinstance(recv_any(ws), bytes):
                pass
            ws.send_json({"type": "start", "topic": "AWS"})
            gate.set()
            while not (isinstance(msg := recv_any(ws), dict) and msg["type"] == "speak_end"):
                pass
            assert msg["cancelled"] is True


def test_speak_disabled_and_empty(setup):
    client, _, _ = setup
    assert client.get("/health").json()["tts"] is False
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["tts"] is None
        ws.send_json({"type": "speak", "text": "Xin chào."})
        assert recv_until(ws, "error")["code"] == "tts_disabled"
        ws.send_json({"type": "speak", "text": "  "})
        assert recv_until(ws, "error")["code"] == "bad_message"


class FakeBridge:
    """Trả lời theo kịch bản deltas. gate: chặn sau delta đầu để test dừng giữa chừng."""

    name = "fake"

    def __init__(self, deltas: list[str], gate: threading.Event | None = None, fail: str | None = None):
        self.deltas, self.gate, self.fail = deltas, gate, fail
        self.session_id: str | None = None
        self.questions: list[str] = []
        self.closed = threading.Event()

    def new_session(self):
        self.session_id = None

    async def ask(self, text):
        self.questions.append(text)
        try:
            for i, d in enumerate(self.deltas):
                if i == 1 and self.gate is not None:
                    await asyncio.to_thread(self.gate.wait, 5)
                yield TextDelta(d)
            if self.fail:
                raise BridgeError(self.fail)
            self.session_id = "s1"
            yield Done("s1", 0.01)
        finally:
            self.closed.set()


ANSWER = ["Chào bạn. EC2 là máy", " ảo chạy trên AWS nhé.\n---CO", "DE---\n```bash\naws ec2 run-instances\n```"]


def bridge_client(tmp_path, bridge, tts=None, auto_ask=True):
    cfg = Config(log_dir=tmp_path / "logs", save_audio=False, min_speech_ms=0, tts_enabled=tts is not None,
                 bridge_auto_ask=auto_ask, bridge_log_dir=tmp_path / "sessions")
    return TestClient(create_app(cfg, engine_factory=lambda _cfg: FakeEngine(), tts_factory=lambda _cfg: tts,
                                 bridge_factory=lambda _cfg: bridge))


def collect_until(ws, kind):
    """Mọi message JSON (bỏ audio) tới khi gặp kind."""
    msgs = []
    while True:
        msg = recv_any(ws)
        if isinstance(msg, dict):
            msgs.append(msg)
            if msg["type"] == kind:
                return msgs


def test_voice_turn_asks_claude_and_speaks(tmp_path):
    bridge, tts = FakeBridge(ANSWER), FakeTts()
    with bridge_client(tmp_path, bridge, tts) as client:
        assert client.get("/health").json()["bridge"] == "fake"
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["bridge"] == {"kind": "fake", "auto_ask": True, "session_id": None,
                                                 "model": None, "models": []}
            ws.send_json({"type": "start", "topic": "AWS"})
            ws.send_bytes(pcm(1.0))
            ws.send_json({"type": "stop"})
            msgs = collect_until(ws, "answer_end")
            msgs += collect_until(ws, "speak_end")
    kinds = [m["type"] for m in msgs]
    assert kinds.index("transcript") < kinds.index("answer_start") < kinds.index("answer_end")
    assert {"type": "state", "value": "WAITING_CLAUDE"} in msgs
    assert [m for m in msgs if m["type"] == "state"][-1]["value"] == "IDLE"
    assert bridge.questions == ["mình muốn hỏi về EC2 và S3"]
    start = next(m for m in msgs if m["type"] == "answer_start")
    assert start["speak_id"] == next(m for m in msgs if m["type"] == "speak_start")["speak_id"]
    assert "".join(m["text"] for m in msgs if m["type"] == "answer_delta") == "".join(ANSWER)
    spoken = [s["source"] for m in msgs if m["type"] == "speak_more" for s in m["sentences"]]
    assert spoken == ["Chào bạn.", "EC2 là máy ảo chạy trên AWS nhé."]  # phần sau ---CODE--- không đọc
    end = next(m for m in msgs if m["type"] == "answer_end")
    assert end["session_id"] == "s1" and end["error"] is None and end["cancelled"] is False
    assert end["first_text_ms"] is not None
    rec = json.loads(next((tmp_path / "sessions").glob("*.jsonl")).read_text(encoding="utf-8"))
    assert rec["question"] == "mình muốn hỏi về EC2 và S3" and rec["answer"] == "".join(ANSWER)
    assert rec["cost_usd"] == 0.01


def test_auto_ask_off_and_typed_ask(tmp_path):
    bridge = FakeBridge(["Được."])
    with bridge_client(tmp_path, bridge, auto_ask=False) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "start", "topic": "AWS"})
            ws.send_bytes(pcm(1.0))
            ws.send_json({"type": "stop"})
            recv_until(ws, "transcript")
            assert recv_until(ws, "state")["value"] == "IDLE"
            ws.send_json({"type": "ask", "text": "S3 là gì?"})
            start = recv_until(ws, "answer_start")
            assert start["speak_id"] is None  # TTS tắt: chỉ có text
            assert recv_until(ws, "answer_end")["error"] is None
    assert bridge.questions == ["S3 là gì?"]


def test_ask_stop_closes_bridge(tmp_path):
    gate = threading.Event()
    bridge = FakeBridge(ANSWER, gate)
    with bridge_client(tmp_path, bridge) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "ask", "text": "EC2 là gì?"})
            recv_until(ws, "answer_delta")
            ws.send_json({"type": "ask_stop"})
            end = recv_until(ws, "answer_end")
            gate.set()
    assert end["cancelled"] is True
    assert bridge.closed.wait(2)  # generator bị đóng = bridge thật kill tiến trình claude


def test_start_interrupts_answer(tmp_path):
    gate = threading.Event()
    bridge = FakeBridge(ANSWER, gate)
    with bridge_client(tmp_path, bridge) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "ask", "text": "EC2 là gì?"})
            recv_until(ws, "answer_delta")
            ws.send_json({"type": "start", "topic": "AWS"})
            assert recv_until(ws, "answer_end")["cancelled"] is True
            gate.set()


def test_bridge_error_reported(tmp_path):
    bridge = FakeBridge(["Đang tìm"], fail="Hết hạn mức")
    with bridge_client(tmp_path, bridge) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "ask", "text": "Giá EC2?"})
            err = recv_until(ws, "error")
            end = recv_until(ws, "answer_end")
            assert recv_until(ws, "state")["value"] == "IDLE"
    assert err["code"] == "bridge_failed" and err["message"] == "Hết hạn mức"
    assert end["error"] == "Hết hạn mức"


def test_new_session_and_disabled(tmp_path, setup):
    bridge = FakeBridge(["Được."])
    bridge.session_id = "old"
    with bridge_client(tmp_path, bridge) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "new_session"})
            assert recv_until(ws, "bridge_session")["session_id"] is None
    client, _, _ = setup
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["bridge"] is None
        ws.send_json({"type": "ask", "text": "S3?"})
        assert recv_until(ws, "error")["code"] == "bridge_disabled"


def test_voice_commands_skip_claude(tmp_path):
    bridge, tts = FakeBridge(["EC2 là máy ảo."]), FakeTts()
    with bridge_client(tmp_path, bridge, tts) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "ask", "text": "Đọc lại đi."})
            cmd = collect_until(ws, "command")[-1]
            assert cmd["name"] == "repeat" and cmd["reply"].startswith("Chưa có")
            collect_until(ws, "speak_end")
            ws.send_json({"type": "ask", "text": "EC2 là gì?"})
            collect_until(ws, "answer_end")
            collect_until(ws, "speak_end")
            ws.send_json({"type": "ask", "text": "Nhắc lại giúp mình"})
            assert collect_until(ws, "command")[-1]["reply"] == "EC2 là máy ảo."
            collect_until(ws, "speak_end")
            ws.send_json({"type": "ask", "text": "Không, mở session mới đi."})
            msgs = collect_until(ws, "command")
            assert {"type": "bridge_session", "session_id": None} in msgs
            assert msgs[-1]["name"] == "new_session"
            collect_until(ws, "speak_end")
    assert bridge.questions == ["EC2 là gì?"]  # lệnh không gửi cho Claude


def test_set_model_applies_to_all_tabs(tmp_path):
    bridge = FakeBridge(["Được."])
    with bridge_client(tmp_path, bridge) as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["bridge"]["model"] is None  # FakeBridge không chọn được model
            ws.send_json({"type": "set_model", "model": "sonnet"})
            assert recv_until(ws, "error")["code"] == "bridge_disabled"
        bridge.model = ""
        with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
            hello = a.receive_json()
            assert hello["bridge"]["model"] == "" and hello["bridge"]["models"] == ["sonnet", "opus", "haiku"]
            recv_until(b, "state")
            a.send_json({"type": "set_model", "model": "gpt"})
            assert recv_until(a, "error")["code"] == "bad_message"
            a.send_json({"type": "set_model", "model": "haiku"})
            assert recv_until(a, "bridge_model")["model"] == "haiku"
            assert recv_until(b, "bridge_model")["model"] == "haiku"
    assert bridge.model == "haiku"


def test_silence_skipped_by_vad(tmp_path):
    engine = FakeEngine()
    cfg = Config(log_dir=tmp_path / "logs", save_audio=False, min_speech_ms=200)
    with TestClient(create_app(cfg, engine_factory=lambda _cfg: engine)) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "start", "topic": "AWS"})
            ws.send_bytes(np.zeros(16000 * 2, dtype="<i2").tobytes())
            ws.send_json({"type": "stop"})
            assert recv_until(ws, "error")["code"] == "no_speech"
            assert recv_until(ws, "state")["value"] == "IDLE"
    assert len(engine.calls) == 1  # chỉ warm-up, không gọi Whisper cho lượt im lặng
