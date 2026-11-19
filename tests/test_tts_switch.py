"""Đổi model TTS từ tab Cài đặt (server/tts_switch.py): engine giả, không nạp model thật."""

from __future__ import annotations

import json
import threading

import numpy as np
from fastapi.testclient import TestClient

from server.app import create_app
from server.config import Config
from server.tts_switch import TtsSwitcher, engine_list

from test_server import FakeEngine, recv_until


class NamedTts:
    """Engine TTS giả mang tên engine. gate: chặn lượt đọc thử (warm-up) để test lúc đang đổi."""

    sample_rate = 24000
    voice = "v"

    def __init__(self, name: str, gate: threading.Event | None = None):
        self.name, self.gate, self.closed = name, gate, False

    def stream(self, text, lang):
        if self.gate is not None:
            self.gate.wait(5)
        yield np.zeros(2400, dtype=np.float32)

    def close(self):
        self.closed = True


def make_client(tmp_path, *, gates=None, broken=(), engines=None):
    """Hai engine a (mặc định) và b. broken: tên engine nạp lỗi. gates: {tên: Event} chặn đọc thử."""
    made: list[NamedTts] = []

    def factory(cfg):
        if cfg.tts_engine in broken:
            raise RuntimeError(f"không nạp được {cfg.tts_engine}")
        tts = NamedTts(cfg.tts_engine, (gates or {}).get(cfg.tts_engine))
        made.append(tts)
        return tts

    cfg = Config(log_dir=tmp_path / "logs", save_audio=False, tts_enabled=True, tts_engine="a",
                 tts_engines=engines or {"a": {"label": "Giọng A"}, "b": {"label": "Giọng B"}},
                 settings_file=tmp_path / "settings.json")
    app = create_app(cfg, engine_factory=lambda _cfg: FakeEngine(), tts_factory=factory)
    return TestClient(app), cfg, made


def switch_messages(ws):
    """Các message tts_switch tới khi lần đổi kết thúc (done/failed)."""
    out = []
    while True:
        msg = recv_until(ws, "tts_switch")
        out.append(msg)
        if msg["job"] and msg["job"]["status"] != "running":
            return out


def test_hello_lists_engines(tmp_path):
    client, _, _ = make_client(tmp_path)
    with client, client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
    assert hello["tts"]["engine"] == "a" and hello["tts"]["label"] == "Giọng A"
    s = hello["settings"]
    assert [e["name"] for e in s["tts_engines"]] == ["a", "b"] and s["tts_switch"] is None
    assert s["stt"]["engine"] == "whisper"


def test_switch_success(tmp_path):
    client, cfg, made = make_client(tmp_path)
    with client:
        with client.websocket_connect("/ws") as ws, client.websocket_connect("/ws") as other:
            ws.receive_json(), other.receive_json()
            ws.send_json({"type": "tts_switch", "engine": "b"})
            msgs = switch_messages(ws)
            assert switch_messages(other)[-1]["job"]["status"] == "done"   # mọi tab đều được báo
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["tts"]["engine"] == "b"
    steps = [m["job"]["step"] for m in msgs]
    assert steps == sorted(steps) and steps[0] == 0 and steps[-1] == len(msgs[-1]["job"]["steps"])
    assert msgs[-1]["tts"]["engine"] == "b" and msgs[-1]["tts"]["label"] == "Giọng B"
    assert made[0].name == "a" and made[0].closed      # model cũ tắt trước khi nạp model mới
    assert cfg.tts_engine == "b"
    assert json.loads((tmp_path / "settings.json").read_text(encoding="utf-8")) == {"tts_engine": "b"}


def test_switch_failure_restores_old(tmp_path):
    client, cfg, made = make_client(tmp_path, broken={"b"})
    with client, client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "tts_switch", "engine": "b"})
        last = switch_messages(ws)[-1]
    job = last["job"]
    assert job["status"] == "failed" and "không nạp được b" in job["error"]
    assert job["failed_step"] == 2 and job["steps"][-1] == "Nạp lại Giọng A"
    assert last["tts"]["engine"] == "a" and cfg.tts_engine == "a"
    assert [t.name for t in made] == ["a", "a"]       # nạp lại a
    assert not (tmp_path / "settings.json").exists()


def test_blocked_while_switching(tmp_path):
    gate = threading.Event()
    client, _, _ = make_client(tmp_path, gates={"b": gate})
    with client, client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "tts_switch", "engine": "b"})
        while recv_until(ws, "tts_switch")["job"]["step"] < 3:   # tới bước đọc thử, đang bị gate chặn
            pass
        with client.websocket_connect("/ws") as late:
            assert late.receive_json()["settings"]["tts_switch"]["engine"] == "b"   # tab mở giữa chừng thấy lớp chờ
        for msg in ({"type": "speak", "text": "Xin chào."}, {"type": "start"}, {"type": "tts_switch", "engine": "a"}):
            ws.send_json(msg)
            assert recv_until(ws, "error")["code"] == "tts_switching"
        gate.set()
        assert switch_messages(ws)[-1]["job"]["status"] == "done"


def test_same_engine_and_unknown(tmp_path):
    client, _, made = make_client(tmp_path)
    with client, client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"type": "tts_switch", "engine": "a"})
        msg = recv_until(ws, "tts_switch")
        assert msg["job"] is None and msg["tts"]["engine"] == "a"
        ws.send_json({"type": "tts_switch", "engine": "zzz"})
        assert recv_until(ws, "error")["code"] == "bad_message"
    assert len(made) == 1


def test_startup_uses_saved_engine(tmp_path):
    (tmp_path / "settings.json").write_text('{"tts_engine": "b"}', encoding="utf-8")
    client, cfg, _ = make_client(tmp_path)
    with client, client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["tts"]["engine"] == "b"
    assert cfg.tts_engine == "b"


def test_startup_saved_engine_broken_falls_back(tmp_path):
    (tmp_path / "settings.json").write_text('{"tts_engine": "b"}', encoding="utf-8")
    client, cfg, _ = make_client(tmp_path, broken={"b"})
    with client, client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["tts"]["engine"] == "a"
    assert cfg.tts_engine == "a"


def test_engine_not_installed(tmp_path):
    cfg = Config(tts_engines={"a": {}, "x": {"python": str(tmp_path / "nope" / "python.exe"), "label": "X"},
                              "w": {"url": "http://127.0.0.1:8790"}})
    items = {e["name"]: e for e in engine_list(cfg)}
    assert items["a"]["available"] and items["a"]["label"] == "a"
    assert not items["x"]["available"] and "chưa cài" in items["x"]["note"]
    assert items["w"]["available"] and "8790" in items["w"]["note"]
    assert "chưa cài" in TtsSwitcher(cfg, None, None, None).check("x")
