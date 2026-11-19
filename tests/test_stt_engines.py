"""Phase 10: chọn nguồn STT và AI bằng config, không nạp model."""

from __future__ import annotations

import pytest

from server.bridge import Done, TextDelta, make_bridge
from server.config import Config, load_config
from server.stt.engine import Transcript
from server.stt.engines import create_stt_engine, stt_engine_class
from server.stt.service import SttService


class FakeStt:
    def __init__(self, reply: str = "xin chào"):
        self.name = "fake"
        self.reply = reply

    def transcribe(self, audio, *, language="vi", initial_prompt=None, hotwords=None, beam_size=5, **_):
        return Transcript(self.reply, language, 1.0)


class FakeAi:
    name = "fake-ai"

    def __init__(self, cfg):
        self.cfg = cfg

    async def ask(self, text, **_):
        yield TextDelta("ok")
        yield Done()


def test_config_stt_engine(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[stt]\nengine = "x:Y"\nmodel = "small"\n[stt."x:Y"]\nreply = "a"\n', encoding="utf-8")
    cfg = load_config(p)
    assert (cfg.stt_engine, cfg.model, cfg.stt_engines) == ("x:Y", "small", {"x:Y": {"reply": "a"}})
    assert load_config().stt_engine == "whisper"
    assert stt_engine_class("whisper").__name__ == "WhisperEngine"
    with pytest.raises(ValueError, match="Không có engine STT"):
        stt_engine_class("khongco")


def test_custom_stt_engine_runs_in_service():
    name = "tests.test_stt_engines:FakeStt"
    cfg = Config(stt_engine=name, stt_engines={name: {"reply": "EC2 là gì"}})
    engine = create_stt_engine(cfg)
    assert engine.reply == "EC2 là gì"
    import numpy as np
    svc = SttService(engine, glossary_path=cfg.glossary, corrections_path=cfg.corrections)
    assert svc.transcribe(np.zeros(16000, dtype=np.float32), svc.topics()[0]).text.startswith("EC2")


def test_custom_bridge_kind():
    cfg = Config(bridge_kind="tests.test_stt_engines:FakeAi")
    bridge = make_bridge(cfg)
    assert bridge.name == "fake-ai" and bridge.cfg is cfg
    assert make_bridge(Config()) is None
    with pytest.raises(ValueError, match="module:Lớp"):
        make_bridge(Config(bridge_kind="khongco"))
