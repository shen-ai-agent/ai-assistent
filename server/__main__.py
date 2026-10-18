"""Khởi động voice server: .venv\\Scripts\\python -m server [--config path] [--open]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys
import webbrowser
from pathlib import Path

import uvicorn

from .app import create_app
from .config import DEFAULT_PATH, load_config


def port_in_use(host: str, port: int) -> bool:
    """Có tiến trình đang nghe ở host:port (server đã chạy từ trước)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def main():
    ap = argparse.ArgumentParser(description="Voice Study Assistant server")
    ap.add_argument("--config", type=Path, default=DEFAULT_PATH)
    ap.add_argument("--open", action="store_true",
                    help="mở trình duyệt khi nạp model xong; server đã chạy sẵn thì chỉ mở trình duyệt")
    args = ap.parse_args()

    # Console Windows mặc định cp1252, log tiếng Việt sẽ lỗi
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_config(args.config)
    url = f"http://{cfg.host}:{cfg.port}/"
    if port_in_use(cfg.host, cfg.port):
        # Không nạp model lần hai: thành hai job GPU cùng lúc, và cổng cũng không bind được
        if args.open:
            logging.info("Server đã chạy ở %s, chỉ mở trình duyệt", url)
            webbrowser.open(url)
            return
        sys.exit(f"Cổng {cfg.port} đang bận: server đã chạy rồi? (tắt theo hướng dẫn trong README)")

    app = create_app(cfg)
    configs = [uvicorn.Config(app, host=cfg.host, port=cfg.port, log_level="info")]
    if cfg.lan_port:
        if cfg.lan_cert.exists() and cfg.lan_key.exists():
            # Cùng một app (chung model, chung khoá GPU), thêm cổng HTTPS cho máy khác trong mạng LAN
            configs.append(uvicorn.Config(app, host="0.0.0.0", port=cfg.lan_port, log_level="info",
                                          ssl_certfile=str(cfg.lan_cert), ssl_keyfile=str(cfg.lan_key)))
        else:
            logging.warning("Chưa có chứng chỉ %s: bỏ qua cổng LAN %d (tạo theo README)", cfg.lan_cert, cfg.lan_port)
    asyncio.run(_serve([uvicorn.Server(c) for c in configs], url if args.open else None))


async def _serve(servers: list[uvicorn.Server], open_url: str | None = None):
    # Chỉ server đầu chạy lifespan (nạp model); cổng LAN mở sau khi nạp xong, dùng chung app.state.
    # Ctrl+C: uvicorn xếp chồng signal handler rồi phát lại tín hiệu khi tắt, nên lần lượt tắt hết.
    main, *extra = servers
    for s in extra:
        s.config.lifespan = "off"
    task = asyncio.create_task(main.serve())
    while not main.started and not task.done():
        await asyncio.sleep(0.1)
    if open_url and main.started:
        logging.info("Đã nạp model, mở %s", open_url)
        webbrowser.open(open_url)
    await asyncio.gather(task, *(s.serve() for s in extra))


if __name__ == "__main__":
    main()
