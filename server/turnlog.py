"""Log mỗi lượt nhận dạng vào _local/logs/ (JSONL theo ngày, kèm wav nếu bật).

Log gồm cả text người dùng sửa tay (event "correction"), làm nguồn cho data/corrections.tsv (T-003).
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

from .protocol import SAMPLE_RATE


def new_turn_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


class TurnLogger:
    def __init__(self, log_dir: Path, save_audio: bool):
        self.dir = log_dir
        self.audio_dir = log_dir / "audio"
        self.save_audio = save_audio

    def turn(self, turn_id: str, audio: np.ndarray, **fields):
        record = {"event": "turn", "turn_id": turn_id, **fields}
        if self.save_audio:
            self.audio_dir.mkdir(parents=True, exist_ok=True)
            path = self.audio_dir / f"{turn_id}.wav"
            sf.write(path, audio, SAMPLE_RATE, subtype="PCM_16")
            record["audio_file"] = str(path.relative_to(self.dir).as_posix())
        self._write(record)

    def wake(self, audio: np.ndarray | None, **fields):
        """Một đoạn nói của chế độ rảnh tay (có tên hay không) vào wake-YYYY-MM-DD.jsonl.
        Audio chỉ lưu khi có (đoạn nhận ra tên), để xem lại các lần đánh thức nhầm."""
        record = {"event": "wake", **fields}
        if audio is not None and self.save_audio:
            wake_dir = self.dir / "wake-audio"
            wake_dir.mkdir(parents=True, exist_ok=True)
            path = wake_dir / f"{new_turn_id()}.wav"
            sf.write(path, audio, SAMPLE_RATE, subtype="PCM_16")
            record["audio_file"] = str(path.relative_to(self.dir).as_posix())
        self._write(record, "wake")

    def correction(self, turn_id: str, text: str):
        self._write({"event": "correction", "turn_id": turn_id, "text": text})

    def _write(self, record: dict, kind: str = "turns"):
        self.dir.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now().isoformat(timespec="milliseconds"), **record}
        path = self.dir / f"{kind}-{datetime.now():%Y-%m-%d}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
