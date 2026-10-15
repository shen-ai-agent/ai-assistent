"""Test vòng WebSocket với engine giả: không cần GPU, chạy vài giây."""

from __future__ import annotations

import json

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
