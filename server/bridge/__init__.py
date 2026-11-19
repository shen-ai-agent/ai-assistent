"""Bridge giữa voice server và Claude (Phase 4). Chọn bằng [bridge] kind trong server/config.toml."""

from __future__ import annotations

from .base import Bridge, BridgeError, Done, TextDelta
from .feeder import SentenceFeeder

__all__ = ["Bridge", "BridgeError", "Done", "TextDelta", "SentenceFeeder", "make_bridge"]


def make_bridge(cfg) -> Bridge | None:
    if cfg.bridge_kind == "none":
        return None
    if cfg.bridge_kind == "claude_cli":
        from .claude_cli import ClaudeCliBridge
        return ClaudeCliBridge(cfg.bridge_workdir, cfg.bridge_prompt, cfg.bridge_tools, cfg.bridge_model,
                               cfg.bridge_timeout_s, generate_timeout_s=cfg.bridge_generate_timeout_s)
    if cfg.bridge_kind == "clipboard":
        from .clipboard import ClipboardBridge
        return ClipboardBridge(cfg.bridge_hotkey)
    if ":" in cfg.bridge_kind:
        # Nguồn AI tự viết (Phase 10): "module:Lớp", lớp nhận Config và cài interface Bridge (base.py)
        import importlib
        module, cls = cfg.bridge_kind.split(":")
        return getattr(importlib.import_module(module), cls)(cfg)
    raise ValueError(f"[bridge] kind = {cfg.bridge_kind!r}: chỉ nhận claude_cli, clipboard, none hoặc module:Lớp")
