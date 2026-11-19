"""RemoteTtsEngine: dùng engine chạy trong worker ngoài tiến trình (server/tts/worker.py) qua HTTP.

Giao thức (chỉ dùng thư viện chuẩn, để worker chạy được trong venv nào cũng được):
- GET /info → JSON {engine, sample_rate, voice, voices, caps, describe}
- POST /synth {text, lang} → chuỗi frame: uint32 LE số byte + float32 LE mono. Frame 0 byte là hết câu;
  ERROR_FRAME rồi uint32 + UTF-8 là lỗi. Đóng kết nối giữa chừng = huỷ, worker dừng ở mẩu kế tiếp.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

import numpy as np

from .base import EngineCaps

log = logging.getLogger("tts")
ROOT = Path(__file__).resolve().parents[3]
ERROR_FRAME = 0xFFFFFFFF
READY = "READY"


def pack_chunk(chunk: np.ndarray) -> bytes:
    data = np.ascontiguousarray(chunk, dtype="<f4").tobytes()
    return struct.pack("<I", len(data)) + data


def pack_end() -> bytes:
    return struct.pack("<I", 0)


def pack_error(message: str) -> bytes:
    data = message.encode("utf-8")
    return struct.pack("<II", ERROR_FRAME, len(data)) + data


def _read_exact(resp, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        part = resp.read(n - len(buf))
        if not part:
            raise ConnectionError("worker TTS ngắt kết nối giữa câu")
        buf += part
    return buf


def _priority_flags() -> int:
    """Worker cùng priority với server (Windows: tiến trình con mặc định về normal)."""
    if sys.platform != "win32":
        return 0
    import ctypes
    from ctypes import wintypes
    # Bản kernel32 riêng, khai báo đủ kiểu: không phụ thuộc chỗ khác đã đổi restype trên ctypes.windll.kernel32
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetPriorityClass.argtypes = [wintypes.HANDLE]
    k32.GetPriorityClass.restype = wintypes.DWORD
    return k32.GetPriorityClass(k32.GetCurrentProcess()) or 0


class RemoteTtsEngine:
    def __init__(self, url: str, proc: subprocess.Popen | None = None, timeout_s: float = 60):
        u = urlparse(url if "://" in url else "http://" + url)
        self.host, self.port = u.hostname or "127.0.0.1", u.port or 80
        self.url = f"http://{self.host}:{self.port}"
        self.proc = proc
        self.timeout_s = timeout_s
        info = self._get("/info")
        self.name = info["engine"]
        self.sample_rate = int(info["sample_rate"])
        self.voice = info.get("voice", "")
        self.caps = EngineCaps.from_dict(info.get("caps", {}))
        self._voices = info.get("voices", [])
        self._describe = info.get("describe", "")

    @classmethod
    def spawn(cls, name: str, options: dict, log_dir: Path | None = None) -> "RemoteTtsEngine":
        """Chạy `python -m server.tts.worker` bằng python của venv model, chờ worker báo READY <port>."""
        from . import local_options
        python = Path(options["python"])
        python = python if python.is_absolute() else ROOT / python
        if not python.exists():
            raise FileNotFoundError(f"[tts.{name}] python không tồn tại: {python}")
        safe = re.sub(r"[^\w.-]+", "_", name)
        log_path = Path(options.get("log") or (log_dir or ROOT / "_local" / "logs") / f"tts-worker-{safe}.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logf = open(log_path, "ab")
        cmd = [str(python), "-m", "server.tts.worker", "--engine", name, "--port", "0", "--parent-pipe",
               "--options", json.dumps(local_options(options), ensure_ascii=False)]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        log.info("Chạy worker TTS %s (%s), log: %s", name, python, log_path)
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=logf, creationflags=_priority_flags())
        port = _wait_ready(proc, logf, float(options.get("start_timeout_s", 600)))
        if port is None:
            _stop(proc, 0)
            logf.close()
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
            raise RuntimeError(f"worker TTS {name} không chạy được (exit {proc.poll()}):\n{tail}")
        try:
            return cls(f"http://127.0.0.1:{port}", proc=proc)
        except Exception:
            _stop(proc)
            raise

    def _get(self, path: str) -> dict:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout_s)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 200:
                raise RuntimeError(f"worker TTS {path}: HTTP {resp.status} {body[:200]!r}")
            return json.loads(body)
        finally:
            conn.close()

    def describe(self) -> str:
        return f"worker {self.url}, {self._describe}"

    def voices(self) -> list[str]:
        return list(self._voices)

    def stream(self, text: str, lang: str) -> Iterator[np.ndarray]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout_s)
        try:
            body = json.dumps({"text": text, "lang": lang}, ensure_ascii=False).encode("utf-8")
            conn.request("POST", "/synth", body, {"Content-Type": "application/json"})
            resp = conn.getresponse()
            if resp.status != 200:
                raise RuntimeError(f"worker TTS: HTTP {resp.status} {resp.read()[:200]!r}")
            while True:
                (n,) = struct.unpack("<I", _read_exact(resp, 4))
                if n == 0:
                    return
                if n == ERROR_FRAME:
                    (m,) = struct.unpack("<I", _read_exact(resp, 4))
                    raise RuntimeError("worker TTS lỗi: " + _read_exact(resp, m).decode("utf-8", "replace"))
                yield np.frombuffer(_read_exact(resp, n), dtype="<f4").astype(np.float32)
        finally:
            conn.close()  # đóng giữa chừng: worker thấy mất kết nối và dừng tổng hợp

    def close(self) -> None:
        """Tắt worker do server chạy (worker nối bằng url thì để nguyên)."""
        if self.proc is not None:
            _stop(self.proc)


def _stop(proc: subprocess.Popen, wait_s: float = 5) -> None:
    """Đóng stdin trước: python.exe của venv trên Windows là launcher sinh thêm tiến trình con, kill() chỉ diệt
    launcher, còn tiến trình con (nạp model) tự thoát khi thấy EOF ở stdin (worker --parent-pipe)."""
    try:
        proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(wait_s)
    except subprocess.TimeoutExpired:
        proc.kill()


def _wait_ready(proc: subprocess.Popen, logf, timeout_s: float) -> int | None:
    """Đọc stdout của worker tới dòng READY <port>. Sau đó vẫn đọc tiếp (chép vào log) để pipe không đầy."""
    found: list[int] = []
    event = threading.Event()

    def pump():
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not found and line.startswith(READY):
                found.append(int(line.split()[1]))
                event.set()
            else:
                logf.write(raw)
                logf.flush()
        event.set()  # worker thoát trước khi sẵn sàng

    threading.Thread(target=pump, name="tts-worker-stdout", daemon=True).start()
    deadline = time.monotonic() + timeout_s
    while not event.wait(0.5):
        if time.monotonic() > deadline:
            break
    return found[0] if found else None
