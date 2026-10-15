"""Đọc data/glossary.txt và dựng prompt gợi ý thuật ngữ cho Whisper."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .textnorm import contains_term, normalize

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "glossary.txt"

# "# ---------- Tiếng Anh: thi cử ----------" → chủ đề "Tiếng Anh" (phần trước dấu ":")
_SECTION = re.compile(r"^#\s*-{3,}\s*(.+?)\s*-{3,}\s*$")


@dataclass
class Term:
    text: str
    priority: bool = False
    aliases: list[str] = field(default_factory=list)
    topic: str = ""


def load_glossary(path: Path = DEFAULT_PATH) -> list[Term]:
    terms = []
    topic = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if m := _SECTION.match(line):
            topic = m.group(1).split(":")[0].strip()
            continue
        if not line or line.startswith("#"):
            continue
        priority = line.startswith("*")
        parts = [p.strip() for p in line.lstrip("*").split("|")]
        terms.append(Term(parts[0], priority, [a for a in parts[1:] if a], topic))
    return terms


def topics(terms: list[Term]) -> list[str]:
    return list(dict.fromkeys(t.topic for t in terms))


def build_prompt(terms: list[Term], style: str, topic: str | None = None) -> str | None:
    """Prompt gợi ý thuật ngữ.

    style:
      none      không dùng prompt
      list      các thuật ngữ ưu tiên (*): "NAT Gateway, Internet Gateway, ..."
      sentence  thuật ngữ ưu tiên trong một câu tự nhiên (Whisper bắt chước văn phong prompt)
      topic     mọi thuật ngữ của một chủ đề; không có chủ đề thì giống list

    Whisper chỉ giữ khoảng 223 token cuối của prompt, nên thuật ngữ ưu tiên đặt sau cùng
    để không bị cắt.
    """
    if style == "none":
        return None
    if style == "topic" and topic:
        chosen = [t for t in terms if t.topic == topic]
        chosen.sort(key=lambda t: t.priority)  # ưu tiên xuống cuối
        return ", ".join(t.text for t in chosen) + "."
    words = [t.text for t in terms if t.priority]
    if style in ("list", "topic"):
        return ", ".join(words) + "."
    if style == "sentence":
        return ("Mình đang học AWS, .NET và luyện IELTS, TOEIC. "
                "Hôm nay mình hỏi về " + ", ".join(words) + ".")
    raise ValueError(f"prompt style không hợp lệ: {style}")


def build_hotwords(terms: list[Term]) -> str:
    return " ".join(t.text for t in terms if t.priority)


def detect_topic(text: str, terms: list[Term]) -> str | None:
    """Chủ đề có nhiều thuật ngữ (kể cả alias) xuất hiện trong text nhất; không có thì None."""
    norm = normalize(text)
    counts = Counter(t.topic for t in terms
                     if any(contains_term(norm, w) for w in [t.text, *t.aliases]))
    return counts.most_common(1)[0][0] if counts else None
