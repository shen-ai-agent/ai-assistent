"""Nguồn TTS đổi bằng config (Phase 10).

`[tts] engine = "<tên>"` chọn engine, bảng `[tts.<tên>]` là tham số riêng của engine đó. Bảng có `python`
(python của venv riêng) thì server chạy engine trong worker ngoài tiến trình; có `url` thì nối tới worker
đã chạy sẵn (`python -m server.tts.worker --engine <tên>`).
"""

from __future__ import annotations

import importlib
from pathlib import Path

from .base import REF_NATIVE_PAUSE_MS, EngineCaps, TtsEngine, as_mono

# Tên → "module:lớp". Import muộn: mỗi engine kéo thư viện riêng, có cái chỉ có trong venv của nó
ENGINES = {
    "zerotts": "server.tts.engines.zerotts:ZeroTtsEngine",
    "vieneu": "server.tts.engines.vieneu:VieNeuEngine",
    "omnivoice": "server.tts.engines.omnivoice:OmniVoiceEngine",
}
# Khoá dành cho server/worker (label: tên hiện ở tab Cài đặt), không truyền vào engine
REMOTE_KEYS = ("python", "url", "start_timeout_s", "log", "label")


def engine_class(name: str):
    """Tên trong ENGINES, hoặc thẳng "module:Lớp" (engine tự viết, chưa đăng ký)."""
    path = ENGINES.get(name) or (name if ":" in name else None)
    if path is None:
        raise ValueError(f"Không có engine TTS '{name}' (có: {', '.join(ENGINES)})")
    module, cls = path.split(":")
    return getattr(importlib.import_module(module), cls)


def local_options(options: dict) -> dict:
    return {k: v for k, v in options.items() if k not in REMOTE_KEYS}


def create_engine(name: str, options: dict | None = None, *, local: bool = False, log_dir: Path | None = None):
    """Tạo engine theo tên. local=True: luôn nạp trong tiến trình này (bỏ qua python/url)."""
    options = dict(options or {})
    if not local and (options.get("url") or options.get("python")):
        from .remote import RemoteTtsEngine
        if options.get("url"):
            return RemoteTtsEngine(options["url"])
        return RemoteTtsEngine.spawn(name, options, log_dir=log_dir)
    return engine_class(name)(**local_options(options))


__all__ = ["ENGINES", "REF_NATIVE_PAUSE_MS", "REMOTE_KEYS", "EngineCaps", "TtsEngine", "as_mono", "create_engine",
           "engine_class", "local_options"]
