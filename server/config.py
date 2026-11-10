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
    # Priority tiến trình trên Windows: normal / above_normal / high. TTS phải gửi kernel GPU đều đặn mỗi 80ms;
    # máy chạy nhiều app thì luồng TTS bị chen nên giọng giật (đo: high giảm số lần hụt audio 91 → 14)
    priority: str = "high"
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
    tts_cuda_graph: bool = True       # CUDA Graph cho local_frame_decode, đỡ giật khi CPU bận (zerotts_gpu.py)
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
    bridge_generate_timeout_s: float = 60  # message generate (client ngoài, không đọc)
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
    # Giao việc cho VS Code (T-011): mặc định tắt để test không mở VS Code; config.toml bật
    agent_enabled: bool = False
    agent_roots: list[Path] = field(default_factory=list)   # mỗi thư mục con là một dự án
    agent_projects_file: Path = ROOT / "_local" / "projects.toml"   # biệt danh, loại trừ, dự án ngoài roots
    agent_claude_dir: Path | None = None   # log phiên Claude Code; None = ~/.claude/projects
    agent_code_exe: str = ""               # rỗng = tìm `code` trong PATH
    agent_auto_submit: bool = True         # tự nhấn Enter vào ô chat Claude; false = chỉ điền sẵn
    agent_window_wait_s: float = 15
    agent_stall_s: float = 12              # tool chưa xong quá chừng này: báo "chờ duyệt hoặc chạy lâu"
    agent_submit_timeout_s: float = 600    # chờ người dùng bấm Enter (khi chỉ điền sẵn)
    agent_task_timeout_s: float = 7200     # thôi theo dõi sau chừng này
    agent_log_dir: Path = ROOT / "_local" / "tasks"
    # Tab Học (T-012): giảng tài liệu, dùng claude của [bridge] (cần kind = "claude_cli")
    study_dir: Path = ROOT / "_local" / "study"          # mỗi tài liệu một thư mục, cũng là cwd của Claude
    study_prompt: Path = ROOT / "prompts" / "study_reply.md"
    study_tools: list[str] = field(default_factory=lambda: ["Read", "WebSearch", "WebFetch"])
    study_timeout_s: float = 300   # lượt đầu phải đọc cả tài liệu
    study_max_chars: int = 200_000


_PATHS = ("bridge_workdir", "bridge_prompt", "bridge_log_dir")


def load_config(path: Path = DEFAULT_PATH) -> Config:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    server, stt, log = raw.get("server", {}), raw.get("stt", {}), raw.get("log", {})
    tts = {f"tts_{k}": v for k, v in raw.get("tts", {}).items()}
    bridge = {f"bridge_{k}": v for k, v in raw.get("bridge", {}).items()}
    bridge.update({k: ROOT / bridge[k] for k in _PATHS if k in bridge})
    assistant = {f"assistant_{k}": v for k, v in raw.get("assistant", {}).items()}
    server.update({k: ROOT / server[k] for k in ("lan_cert", "lan_key") if k in server})
    agent = {f"agent_{k}": v for k, v in raw.get("agent", {}).items()}
    study = {f"study_{k}": v for k, v in raw.get("study", {}).items()}
    study.update({k: ROOT / study[k] for k in ("study_dir", "study_prompt") if k in study})
    agent.update({k: ROOT / agent[k] for k in ("agent_projects_file", "agent_log_dir") if k in agent})
    if "agent_roots" in agent:
        agent["agent_roots"] = [Path(p) for p in agent["agent_roots"]]
    if agent.get("agent_claude_dir"):
        agent["agent_claude_dir"] = Path(agent["agent_claude_dir"]).expanduser()
    else:
        agent.pop("agent_claude_dir", None)
    cfg = Config(**server, **stt, **tts, **bridge, **assistant, **agent, **study)
    if "dir" in log:
        cfg.log_dir = ROOT / log["dir"]
    cfg.save_audio = log.get("save_audio", cfg.save_audio)
    return cfg
