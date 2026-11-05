"""Bỏ câu Whisper bịa ra khi audio là tiếng ồn hoặc im lặng.

Whisper học từ phụ đề YouTube nên gặp audio không rõ tiếng thì viết câu cuối video: "Cảm ơn các bạn đã
theo dõi và hẹn gặp lại.", "Hãy subscribe cho kênh ... để không bỏ lỡ những video hấp dẫn". Các mẫu dưới
đây lấy từ log lượt nói thật (_local/logs). Chỉ bỏ cả câu khớp mẫu, câu khác trong lượt giữ nguyên.
"""

from __future__ import annotations

import re

from ..assistant.wake import fold

# So trên chuỗi đã bỏ dấu, chữ thường, gộp chữ lặp (fold từng từ, nối bằng dấu cách)
_PATTERNS = [re.compile(p) for p in (
    r"^cam on cac ban da (theo doi|xem)\b",
    r"^hen gap lai( cac ban)?\b",
    r"\bsubscribe cho kenh\b",
    r"\bdang ki kenh\b",
    r"\bkhong bo lo nhung video\b",
    r"\blike va subscribe\b",
    r"^thank(s| iou) for watching\b",     # fold đổi y → i: "you" → "iou", "by" → "bi"
    r"^subtitles? bi\b",
)]
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")   # chỉ tách khi sau dấu câu là khoảng trắng ("app.js" giữ nguyên)
_WORD = re.compile(r"\w+")


def is_hallucination(sentence: str) -> bool:
    s = " ".join(fold(w) for w in _WORD.findall(sentence))
    return bool(s) and any(p.search(s) for p in _PATTERNS)


def drop_hallucinations(text: str) -> str:
    """Bỏ các câu khớp mẫu ảo giác; cả lượt chỉ toàn câu ảo giác thì trả "". Không câu nào bị bỏ thì
    trả nguyên text."""
    sentences = [s for s in _SENTENCE_END.split(text.strip()) if s]
    kept = [s for s in sentences if not is_hallucination(s)]
    return text if len(kept) == len(sentences) else " ".join(kept)
