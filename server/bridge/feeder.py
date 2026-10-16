"""Cắt text Claude stream về thành từng đoạn đọc được ngay, để câu đầu không phải chờ cả câu trả lời.

Cắt ở dấu kết câu + khoảng trắng hoặc ở xuống dòng, nhưng không cắt trong code block (```).
Gặp CODE_MARKER thì ngừng: phần sau marker chỉ để hiện trên màn hình. Mỗi đoạn trả ra còn qua
TtsService.plan (clean_structure, tách câu, chuẩn hoá) nên cắt hơi thừa cũng không sao.
"""

from __future__ import annotations

import re

from ..tts.normalize import CODE_MARKER

FENCE = "```"
# Dấu kết câu theo sau là khoảng trắng; không khớp "3.5" hay "file.py" vì sau dấu chấm không có khoảng trắng
_BOUNDARY = re.compile(r"[.!?…:;](?=\s)|\n")


class SentenceFeeder:
    def __init__(self):
        self.buf = ""
        self.stopped = False

    def push(self, delta: str) -> list[str]:
        if self.stopped:
            return []
        self.buf += delta
        out: list[str] = []
        i = self.buf.find(CODE_MARKER)
        if i >= 0:
            head, self.buf, self.stopped = self.buf[:i], "", True
            return [head] if head.strip() else []
        # Marker đang đến dở ("---CO") nằm sau ranh giới cắt nên vẫn ở lại buffer, lần sau ghép đủ
        cut = self._last_boundary(self.buf)
        if cut > 0:
            head, self.buf = self.buf[:cut], self.buf[cut:]
            if head.strip():
                out.append(head)
        return out

    def flush(self) -> list[str]:
        head, self.buf = self.buf, ""
        if self.stopped or not head.strip():
            return []
        if head.count(FENCE) % 2:
            head += "\n" + FENCE  # code block chưa đóng: đóng lại để clean_structure bỏ đúng phần code
        return [head]

    def _last_boundary(self, text: str) -> int:
        """Vị trí cắt muộn nhất nằm ngoài code block; 0 = chưa cắt được."""
        best = 0
        for m in _BOUNDARY.finditer(text):
            end = m.end()
            if text.count(FENCE, 0, end) % 2 == 0:
                best = end
        return best
