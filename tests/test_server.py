"""Test vòng WebSocket với engine giả: không cần GPU, chạy vài giây."""

from __future__ import annotations

import json
import threading

import numpy as np
import pytest
from fastapi.testclient import TestClient

from server.app import create_app
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
            frames, sentences = [], []
            while True:
                msg = recv_any(ws)
                if isinstance(msg, bytes):
                    frames.append(msg)
                elif msg["type"] == "speak_start":
                    start = msg
                elif msg["type"] == "speak_sentence":
                    sentences.append(msg["index"])
                elif msg["type"] == "speak_end":
                    end = msg
                    break
    assert [s["source"] for s in start["sentences"]] == TWO_SRC
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
