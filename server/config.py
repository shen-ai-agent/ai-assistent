"""Đọc server/config.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = Path(__file__).resolve().parent / "config.toml"


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8765
    # Dùng từ điện thoại cùng Wi-Fi: thêm cổng HTTPS (mic trên trình duyệt cần HTTPS). 0 = tắt
    lan_port: int = 0
    lan_cert: Path = ROOT / "_local" / "certs" / "lan.crt"
    lan_key: Path = ROOT / "_local" / "certs" / "lan.key"
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
    tts_first_chunk_frames: int = 1   # 1 frame = 80ms audio
    tts_max_chunk_frames: int = 16
    # Bridge tới Claude: mặc định "none" để test không gọi Claude; config.toml chọn claude_cli/clipboard
    bridge_kind: str = "none"
    bridge_auto_ask: bool = True  # transcript xong thì tự hỏi Claude
    bridge_hotkey: str = "ctrl+alt+r"
    bridge_workdir: Path = ROOT / "_local" / "claude-workdir"
    bridge_prompt: Path = ROOT / "prompts" / "voice_reply.md"
    bridge_tools: list[str] = field(default_factory=lambda: ["WebSearch", "WebFetch"])
    bridge_model: str = ""  # rỗng = model mặc định của Claude Code
    bridge_models: list[str] = field(default_factory=lambda: ["sonnet", "opus", "haiku"])  # lựa chọn trên web
    bridge_timeout_s: float = 120
    bridge_log_dir: Path = ROOT / "_local" / "sessions"
    # Trợ lý rảnh tay (T-010): mic luôn mở, gọi tên mới nghe câu hỏi
    assistant_name: str = "Mimi"
    assistant_aliases: list[str] = field(default_factory=lambda: ["mi mi"])
    assistant_acks: list[str] = field(default_factory=lambda: ["Có mình đây.", "Dạ?", "Mình nghe nè.", "Bạn cần gì nè?"])
    assistant_wake_min_score: float = 0.8   # độ giống tối thiểu của tên (difflib, sau khi bỏ dấu)
    assistant_wake_prompt: str = ""         # initial_prompt lúc tìm tên (rỗng = không prompt)
    assistant_wake_window_s: float = 3.0    # chỉ nhận dạng chừng này giây đầu đoạn nói để tìm tên
    assistant_min_rest_words: int = 2       # sau tên có từng này từ thì hỏi luôn, không đáp
    assistant_ack_grace_ms: int = 300       # chỉ gọi tên: chờ thêm trước khi đáp; nói tiếp ("Mimi ơi… EC2 là gì") là câu hỏi
    assistant_sleep_silence_ms: int = 500  # im lặng bao lâu thì hết đoạn, lúc chờ tên
    assistant_active_silence_ms: int = 900  # như trên, lúc nghe câu hỏi (người nói hay ngập ngừng)
    assistant_active_timeout_s: float = 8   # đáp xong mà không nói gì trong chừng này giây thì ngủ lại
    assistant_follow_up_s: float = 6        # đọc xong câu trả lời, hỏi tiếp không cần gọi tên (0 = tắt)
    assistant_max_segment_s: float = 30


_PATHS = ("bridge_workdir", "bridge_prompt", "bridge_log_dir")


def load_config(path: Path = DEFAULT_PATH) -> Config:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    server, stt, log = raw.get("server", {}), raw.get("stt", {}), raw.get("log", {})
    tts = {f"tts_{k}": v for k, v in raw.get("tts", {}).items()}
    bridge = {f"bridge_{k}": v for k, v in raw.get("bridge", {}).items()}
    bridge.update({k: ROOT / bridge[k] for k in _PATHS if k in bridge})
    assistant = {f"assistant_{k}": v for k, v in raw.get("assistant", {}).items()}
    server.update({k: ROOT / server[k] for k in ("lan_cert", "lan_key") if k in server})
    cfg = Config(**server, **stt, **tts, **bridge, **assistant)
    if "dir" in log:
        cfg.log_dir = ROOT / log["dir"]
    cfg.save_audio = log.get("save_audio", cfg.save_audio)
    return cfg
