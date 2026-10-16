"""Log từng lượt hỏi Claude: _local/sessions/YYYY-MM-DD.jsonl (câu hỏi, câu trả lời, thời gian, chi phí, lỗi)."""

from __future__ import annotations

import json
import time
from pathlib import Path


class AskLogger:
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir

    def write(self, **rec) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **rec}
        path = self.log_dir / f"{time.strftime('%Y-%m-%d')}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
