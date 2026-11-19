"""Phase 10: registry engine TTS, config [tts.<engine>], worker ngoài tiến trình + RemoteTtsEngine.

Dùng engine giả, không nạp model."""

from __future__ import annotations

import sys
import threading
import time
from http.server import ThreadingHTTPServer

import numpy as np
import pytest

from server.config import load_config
from server.tts.engines import EngineCaps, REF_NATIVE_PAUSE_MS, create_engine, engine_class
from server.tts.engines.remote import RemoteTtsEngine
from server.tts.synth import TtsService
from server.tts.worker import make_handler

FAKE = "tests.test_tts_engines:FakeTts"


class FakeTts:
    """Mỗi câu ra n mẩu, mẩu thứ i toàn giá trị i/100. fail_at: ném lỗi ở mẩu đó."""

    caps = EngineCaps(streaming=True, native_pause_ms=40)
    closed: list[str] = []

    def __init__(self, voice: str = "a", chunks: int = 3, size: int = 160, fail_at: int = -1, delay_s: float = 0):
        self.voice, self.n, self.size, self.fail_at, self.delay_s = voice, chunks, size, fail_at, delay_s
        self.sample_rate = 16000

    def voices(self):
        return ["a", "b"]

    def stream(self, text, lang):
        try:
            for i in range(self.n):
                if i == self.fail_at:
                    raise ValueError("hỏng")
                if self.delay_s:
                    time.sleep(self.delay_s)
                yield np.full(self.size, i / 100, dtype=np.float32)
        finally:
            FakeTts.closed.append(text)


def test_registry_by_name_and_path():
    assert engine_class("zerotts").__name__ == "ZeroTtsEngine"
    assert engine_class(FAKE).__name__ == "FakeTts"  # pytest nạp file này dưới tên module khác
    with pytest.raises(ValueError, match="Không có engine"):
        engine_class("khongco")
    eng = create_engine(FAKE, {"voice": "b", "python": "bo-qua.exe"}, local=True)  # local: bỏ khoá của worker
    assert eng.voice == "b"


def test_config_engine_tables(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[tts]\nenabled = true\nengine = "vieneu"\npause_scale = 0.5\n'
                 '[tts.zerotts]\nvoice = "maichi"\n[tts.vieneu]\nvoice = "x"\npython = "py.exe"\n', encoding="utf-8")
    cfg = load_config(p)
    assert cfg.tts_engine == "vieneu" and cfg.tts_pause_scale == 0.5
    assert cfg.tts_options == {"voice": "x", "python": "py.exe"}
    assert cfg.tts_engines["zerotts"] == {"voice": "maichi"}
    p.write_text('[tts]\nvoice = "maichi"\n', encoding="utf-8")  # cấu hình phẳng kiểu cũ
    with pytest.raises(ValueError, match=r"\[tts\.<engine>\]"):
        load_config(p)


def test_bundled_config_loads():
    cfg = load_config()
    assert cfg.tts_engine in cfg.tts_engines
    engine_class(cfg.tts_engine)


def test_service_compensates_native_pause():
    text = "Câu một. Câu hai?\n\nĐoạn mới."
    base = TtsService(object()).plan(text)                    # engine không khai caps: như ZeroTTS
    svc = TtsService(FakeTts())                               # lặng sẵn 40ms
    extra = REF_NATIVE_PAUSE_MS - 40
    assert svc.pause_extra == extra
    assert [s.pause_ms for s in svc.plan(text)] == [p.pause_ms + extra for p in base]
    assert svc.seam_pause("xong.\n", "\nĐoạn mới.") == TtsService(object()).seam_pause("xong.\n", "\nĐoạn mới.") + extra
    assert svc.seam_pause("xong.", " tiếp") == 0
    assert TtsService(FakeTts(), pause_scale=0).pause_extra == 0


@pytest.fixture
def worker():
    """Worker HTTP chạy trong luồng, engine giả."""
    servers = []

    def start(**opts):
        eng = FakeTts(**opts)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler("fake", eng, threading.Lock()))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return RemoteTtsEngine(f"http://127.0.0.1:{srv.server_address[1]}", timeout_s=5)

    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def test_remote_engine_streams_chunks(worker):
    eng = worker(voice="b", chunks=3)
    assert (eng.sample_rate, eng.voice, eng.voices()) == (16000, "b", ["a", "b"])
    assert eng.caps == FakeTts.caps
    chunks = list(eng.stream("Xin chào.", "vi"))
    assert [len(c) for c in chunks] == [160] * 3
    assert [float(c[0]) for c in chunks] == pytest.approx([0, 0.01, 0.02])
    assert all(c.dtype == np.float32 for c in chunks)
    # Câu có dấu, nhiều câu nối tiếp trên cùng worker
    assert len(list(eng.stream("Câu thứ hai, tiếng Việt có dấu.", "vi"))) == 3


def test_remote_engine_error(worker):
    eng = worker(chunks=3, fail_at=1)
    it = eng.stream("Lỗi giữa câu.", "vi")
    assert len(next(it)) == 160
    with pytest.raises(RuntimeError, match="hỏng"):
        next(it)


def test_remote_cancel_stops_worker(worker):
    eng = worker(chunks=200, size=16000, delay_s=0.01)
    it = eng.stream("Huỷ giữa chừng.", "vi")
    next(it)
    it.close()  # như server khi người dùng bấm dừng
    for _ in range(100):
        if "Huỷ giữa chừng." in FakeTts.closed:
            break
        time.sleep(0.05)
    assert "Huỷ giữa chừng." in FakeTts.closed


def test_spawn_worker_process(tmp_path):
    """Server tự chạy worker bằng python chỉ định, chờ READY, close() tắt worker."""
    eng = create_engine(FAKE, {"python": sys.executable, "voice": "b", "chunks": 2, "start_timeout_s": 60},
                        log_dir=tmp_path)
    try:
        assert eng.proc is not None and eng.voice == "b"
        assert len(list(eng.stream("Một câu.", "vi"))) == 2
    finally:
        eng.close()
    assert eng.proc.poll() is not None
    assert list(tmp_path.glob("tts-worker-*.log"))


def test_spawn_worker_failure(tmp_path):
    with pytest.raises(RuntimeError, match="không chạy được"):
        create_engine("tests.test_tts_engines:KhongCo", {"python": sys.executable, "start_timeout_s": 60},
                      log_dir=tmp_path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows")
def test_priority_flags_ignores_shared_kernel32():
    """server/__main__ từng đổi restype trên ctypes.windll.kernel32 (dùng chung), làm _priority_flags tràn int."""
    import ctypes

    from server.tts.engines.remote import _priority_flags
    fn = ctypes.windll.kernel32.GetCurrentProcess
    old = fn.restype
    fn.restype = ctypes.c_void_p
    try:
        assert _priority_flags() in (0x20, 0x40, 0x80, 0x4000, 0x8000)
    finally:
        fn.restype = old
