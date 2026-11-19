"""Worker TTS ngoài tiến trình (Phase 10): nạp một engine và phục vụ qua HTTP (giao thức trong engines/remote.py).

Chạy bằng python của venv có model, từ gốc repo:

    _local/venvs/vieneu/Scripts/python -m server.tts.worker --engine vieneu --port 8790

Tham số engine lấy từ bảng [tts.<engine>] của server/config.toml (hoặc --options JSON). Server tự chạy worker
khi bảng có `python`; nối tới worker chạy tay khi bảng có `url = "http://127.0.0.1:8790"`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("tts.worker")


def make_handler(name: str, engine, lock: threading.Lock):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            # Mẩu audio nhỏ gửi liên tục: tắt Nagle để không bị giữ lại chờ gộp gói
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _json(self, obj, status: int = 200):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/info":
                describe = getattr(engine, "describe", None)
                self._json({"engine": name, "sample_rate": engine.sample_rate, "voice": getattr(engine, "voice", ""),
                            "voices": engine.voices(), "caps": engine.caps.to_dict(),
                            "describe": describe() if describe else "", "pid": os.getpid()})
            elif self.path == "/health":
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            from .engines.remote import pack_chunk, pack_end, pack_error
            if self.path != "/synth":
                return self._json({"error": "not found"}, 404)
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            text, lang = req.get("text", ""), req.get("lang", "vi")
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.end_headers()
            t0, n = time.perf_counter(), 0
            with lock:  # một câu một lúc, như khoá TTS của server
                it = engine.stream(text, lang)
                try:
                    for chunk in it:
                        self.wfile.write(pack_chunk(chunk))
                        n += len(chunk)
                    self.wfile.write(pack_end())
                except (ConnectionError, OSError):
                    log.info("Huỷ giữa câu (%.0fms audio)", n * 1000 / engine.sample_rate)
                except Exception as e:
                    log.exception("Lỗi tổng hợp")
                    try:
                        self.wfile.write(pack_error(f"{type(e).__name__}: {e}"))
                    except OSError:
                        pass
                finally:
                    it.close()
            log.debug("%d ký tự → %.1fs audio trong %.2fs", len(text), n / engine.sample_rate,
                      time.perf_counter() - t0)

    return Handler


def _exit_with_parent():
    """Server chạy worker với stdin là pipe: server tắt (kể cả bị kill) thì pipe đóng, worker thoát theo.

    Chỉ gọi sau khi nạp model xong. Trên Windows, lệnh đọc đồng bộ đang chờ trên pipe chặn mọi thao tác khác trên
    cùng handle: thư viện nào chạm vào stdin lúc import/nạp model (VieNeu) sẽ treo luôn. Server tắt giữa lúc nạp
    thì pipe đã đóng, nạp xong đọc được EOF ngay và worker thoát."""
    try:
        sys.stdin.buffer.read()
    finally:
        os._exit(0)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", required=True)
    ap.add_argument("--options", help="JSON tham số engine (mặc định: bảng [tts.<engine>] của config)")
    ap.add_argument("--config", type=Path, help="file config (mặc định server/config.toml)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8790, help="0 = cổng trống bất kỳ")
    ap.add_argument("--parent-pipe", action="store_true", help="thoát khi stdin đóng (server tự chạy worker)")
    args = ap.parse_args()

    from .engines import create_engine
    if args.options is not None:
        options = json.loads(args.options)
    else:
        from ..config import DEFAULT_PATH, load_config
        options = load_config(args.config or DEFAULT_PATH).tts_engines.get(args.engine, {})
    t = time.perf_counter()
    engine = create_engine(args.engine, options, local=True)
    # Lượt đầu thường chậm (cấp phát GPU, dựng graph): chạy trước khi nhận việc
    for _ in engine.stream("Xin chào bạn.", "vi"):
        pass
    describe = getattr(engine, "describe", lambda: "")()
    log.info("Engine %s sẵn sàng sau %.1fs (%s, %dHz)", args.engine, time.perf_counter() - t, describe,
             engine.sample_rate)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.engine, engine, threading.Lock()))
    if args.parent_pipe:
        threading.Thread(target=_exit_with_parent, name="parent-pipe", daemon=True).start()
    print(f"READY {server.server_address[1]}", flush=True)
    log.info("Nghe ở http://%s:%d", args.host, server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
