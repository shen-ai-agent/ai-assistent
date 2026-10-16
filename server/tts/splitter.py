"""Tách văn xuôi thành câu để tổng hợp và phát từng câu (câu sau tổng hợp trong lúc câu trước đang phát).

Chỉ tách ở dấu câu có khoảng trắng phía sau, nên "3.11", "app.py" không bị tách. Viết tắt có
dấu chấm ("v.v.", "e.g.") cũng không.
"""

from __future__ import annotations

import re
from typing import Callable

MIN_LEN = 20     # câu ngắn hơn thì gộp với câu bên cạnh (TTS câu quá ngắn nghe cụt, hay lỗi)
MAX_LEN = 200    # câu dài hơn thì cắt ở dấu phẩy
FIRST_MAX = 80   # câu đầu ngắn hơn bình thường để có tiếng sớm

_ABBREV = {"v.v.", "vv.", "e.g.", "i.e.", "etc.", "vs.", "mr.", "mrs.", "dr.", "tp.", "st."}
_ENDING_ABBREV = {"v.v.", "vv.", "etc."}
_END = re.compile(r"[.!?;…]+(?=\s|$)")


def _raw_split(text: str) -> list[str]:
    out, start = [], 0
    for m in _END.finditer(text):
        word = text[:m.end()].rsplit(None, 1)[-1].lower()
        nxt = text[m.end():].lstrip()[:1]
        # "v.v." cuối câu (sau là chữ hoa) vẫn là hết câu
        if (word in _ABBREV and not (word in _ENDING_ABBREV and nxt.isupper())) or re.fullmatch(r"[a-z]\.", word):
            continue
        out.append(text[start:m.end()].strip())
        start = m.end()
    out.append(text[start:].strip())
    return [s for s in out if s]


def _cut(sentence: str, limit: int) -> list[str]:
    """Cắt câu dài ở dấu phẩy, mỗi phần không quá limit (nếu được)."""
    if len(sentence) <= limit:
        return [sentence]
    parts = re.split(r"(?<=,)\s+", sentence)
    out, cur = [], ""
    for p in parts:
        if cur and len(cur) + 1 + len(p) > limit:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    out.append(cur)
    return out


def split_sentences(text: str, *, min_len: int = MIN_LEN, max_len: int = MAX_LEN, first_max: int = FIRST_MAX,
                    key: Callable[[str], str] | None = None) -> list[str]:
    """key: chỉ gộp hai câu khi key giống nhau (vd ngôn ngữ, để không trộn câu Việt với câu Anh)."""
    sents = [part for s in _raw_split(text) for part in _cut(s, max_len)]

    merged: list[str] = []
    for s in sents:
        if merged and (len(merged[-1]) < min_len or len(s) < min_len) \
                and len(merged[-1]) + len(s) < max_len and (key is None or key(merged[-1]) == key(s)):
            merged[-1] = f"{merged[-1]} {s}"
        else:
            merged.append(s)

    if merged and len(merged[0]) > first_max:
        first = _cut(merged[0], first_max)
        # Chỉ tách nếu phần đầu không quá ngắn
        if len(first) > 1 and len(first[0]) >= min_len:
            merged[0:1] = [first[0], " ".join(first[1:])]
    return merged
