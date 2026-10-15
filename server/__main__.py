"""Khởi động voice server: .venv\\Scripts\\python -m server [--config path]"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import uvicorn

from .app import create_app
from .config import DEFAULT_PATH, load_config


def main():
    ap = argparse.ArgumentParser(description="Voice Study Assistant server")
    ap.add_argument("--config", type=Path, default=DEFAULT_PATH)
    args = ap.parse_args()

    # Console Windows mặc định cp1252, log tiếng Việt sẽ lỗi
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_config(args.config)
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
