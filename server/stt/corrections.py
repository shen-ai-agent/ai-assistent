"""Sửa lỗi STT lặp lại theo bảng data/corrections.tsv.

Mỗi dòng: <cách STT viết sai> TAB <cách viết đúng>. So khớp không phân biệt
hoa/thường, đúng ranh giới từ (không thay bên trong từ khác).
"""

from __future__ import annotations

import re
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "corrections.tsv"


class Corrector:
    def __init__(self, rules: list[tuple[str, str]]):
        # Pattern dài trước để "cloud front" không bị rule ngắn hơn nuốt mất
        rules = sorted(rules, key=lambda r: len(r[0]), reverse=True)
        self.rules = [(re.compile(rf"(?<!\w){re.escape(src)}(?!\w)", re.I), dst) for src, dst in rules]

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> "Corrector":
        rules = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip() or line.startswith("#"):
                    continue
                src, _, dst = line.partition("\t")
                if src.strip() and dst.strip():
                    rules.append((src.strip(), dst.strip()))
        return cls(rules)

    def apply(self, text: str) -> str:
        for pattern, dst in self.rules:
            text = pattern.sub(dst, text)
        return text
