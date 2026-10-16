"""Đọc server/config.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = Path(__file__).resolve().parent / "config.toml"


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8765
    model: str = "large-v3"
    compute_type: str = "int8_float16"
    device: str = "cuda"
    language: str = "vi"
    beam_size: int = 5
    min_audio_ms: int = 300
    min_speech_ms: int = 200
    max_audio_s: int = 120
    log_dir: Path = ROOT / "_local" / "logs"
    save_audio: bool = True
    glossary: Path = ROOT / "data" / "glossary.txt"
    corrections: Path = ROOT / "data" / "corrections.tsv"
    # TTS: mặc định tắt để test không load model; config.toml bật
    tts_enabled: bool = False
    tts_voice: str = "maichi"
    tts_device: str = "cuda"
    tts_threads: int = 4
    tts_loanwords: bool = False


def load_config(path: Path = DEFAULT_PATH) -> Config:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    server, stt, log = raw.get("server", {}), raw.get("stt", {}), raw.get("log", {})
    tts = {f"tts_{k}": v for k, v in raw.get("tts", {}).items()}
    cfg = Config(**server, **stt, **tts)
    if "dir" in log:
        cfg.log_dir = ROOT / log["dir"]
    cfg.save_audio = log.get("save_audio", cfg.save_audio)
    return cfg
