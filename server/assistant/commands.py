"""Lệnh giọng nói chạy ngay trên server, không hỏi Claude: "mở session mới", "dừng lại", "đọc lại".

Claude không điều khiển được app, nên hỏi "mở session mới đi" thì Claude chỉ trả lời chung chung.
Chỉ câu ngắn mới là lệnh: "đọc lại câu vừa rồi chậm hơn được không" vẫn là câu hỏi cho Claude.
So khớp sau fold (bỏ dấu, gộp chữ lặp) như WakeMatcher.
"""

from __future__ import annotations

import re
from enum import StrEnum

from .wake import fold

_WORD = re.compile(r"\w+")


class Command(StrEnum):
    NEW_SESSION = "new_session"
    STOP = "stop"
    REPEAT = "repeat"


def _f(phrases: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(" ".join(fold(w) for w in _WORD.findall(p)) for p in phrases)


# Session mới: đứng một mình là đủ
_NEW_ALONE = _f(("xoá ngữ cảnh", "xóa ngữ cảnh", "quên hết", "new session", "new chat"))
# ... hoặc "<tên> mới" đi cùng động từ: "tạo section chat mới" (Whisper hay viết session thành section)
_NEW_NOUN = _f(("session mới", "section mới", "chat mới", "phiên mới", "cuộc trò chuyện mới", "trò chuyện mới",
                "hội thoại mới", "chủ đề mới", "context mới"))
_NEW_VERB = set(_f(("mở", "tạo", "bắt", "làm", "sang", "qua", "chuyển", "đổi", "reset", "start", "open")))
# Câu hỏi thật: "làm sao tạo session mới trong ASP.NET"
_QUESTION = _f(("làm sao", "thế nào", "là gì", "cách", "trong", "how", "what", "why"))
_NEW_MAX_WORDS = 8
_REPEAT = _f(("đọc lại", "nói lại", "nhắc lại", "repeat", "say again"))
_REPEAT_MAX_WORDS = 5
# Dừng: cả câu chỉ có từng này (thêm từ đệm), vì "dừng" hay nằm trong câu hỏi thật
_STOP = set(_f(("dừng", "dừng lại", "dừng đi", "thôi", "thôi được rồi", "thôi dừng lại", "im đi", "stop",
                "ok dừng", "ok thôi", "được rồi dừng lại")))
_FILLERS = set(_f(("ơi", "à", "nhé", "nha", "đi", "đã", "giúp", "mình", "cho", "với", "please", "ok", "oke")))


def _has(joined: str, phrases: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(p)}\b", joined) for p in phrases)


def match_command(text: str) -> Command | None:
    words = [fold(w) for w in _WORD.findall(text)]
    if not words:
        return None
    joined = " ".join(words)
    core = " ".join(w for w in words if w not in _FILLERS)
    if joined in _STOP or core in _STOP:
        return Command.STOP
    if len(words) <= _NEW_MAX_WORDS and not _has(joined, _QUESTION):
        if _has(joined, _NEW_ALONE):
            return Command.NEW_SESSION
        # Không động từ thì chỉ nhận câu rất ngắn ("session mới đi"), tránh "tính năng chat mới của Teams"
        if _has(joined, _NEW_NOUN) and (_NEW_VERB & set(words) or len(core.split()) <= 3):
            return Command.NEW_SESSION
    if len(words) <= _REPEAT_MAX_WORDS and _has(joined, _REPEAT):
        return Command.REPEAT
    return None
