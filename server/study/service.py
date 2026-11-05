"""Giảng tài liệu (tab Học, T-012): mỗi tài liệu một bridge Claude riêng (cwd = thư mục tài liệu, session riêng),
để câu hỏi ở tab Học không lẫn vào cuộc trò chuyện ở tab Mimi và mở lại tài liệu cũ thì học tiếp được.

Lượt đầu (chưa có session) gửi kèm lời dặn đọc doc.md; các lượt sau Claude đã có tài liệu trong ngữ cảnh.
Tiến độ (dòng `[Phần k/N: …]`) và lịch sử lưu ngay khi có, session chỉ lưu khi lượt trả lời xong
(giống ClaudeCliBridge: lượt bị ngắt không thành ngữ cảnh).
"""

from __future__ import annotations

from contextlib import aclosing
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

from ..bridge.base import Bridge, Done, TextDelta
from .library import Library, MarkerFilter, StudyDoc

TutorFactory = Callable[[Path], Bridge]


def first_prompt(doc: StudyDoc, question: str) -> str:
    return (f'Tài liệu học mới: "{doc.title}" ({doc.chars:,} ký tự), nằm trong file doc.md ở thư mục hiện tại. '
            "Đọc hết file bằng tool Read (dài thì đọc tiếp bằng offset cho tới hết), "
            "rồi giảng theo hướng dẫn: chia phần và bắt đầu từ phần 1.\n\n"
            f"Mình nói: {question}")


class StudyService:
    def __init__(self, library: Library, tutor_factory: TutorFactory):
        self.library = library
        self.tutor_factory = tutor_factory
        self._tutors: dict[str, Bridge] = {}

    def tutor(self, doc: StudyDoc) -> Bridge:
        t = self._tutors.get(doc.id)
        if t is None:
            t = self._tutors[doc.id] = self.tutor_factory(self.library.workdir(doc.id))
            t.session_id = doc.session_id
        return t

    def forget(self, doc_id: str) -> None:
        """Bỏ bridge đã tạo (học lại từ đầu, xoá tài liệu)."""
        self._tutors.pop(doc_id, None)

    async def ask(self, doc_id: str, question: str, model: str | None = None,
                  on_doc: Callable[[StudyDoc], Awaitable[None]] | None = None) -> AsyncIterator[TextDelta | Done]:
        """Như Bridge.ask, nhưng cắt dòng tiến độ và ghi tiến độ/lịch sử. on_doc: báo web khi tiến độ đổi."""
        doc = self.library.get(doc_id)
        tutor = self.tutor(doc)
        if model is not None and hasattr(tutor, "model"):
            tutor.model = model   # theo ô chọn model chung của web
        prompt = question if tutor.session_id else first_prompt(doc, question)
        marker = MarkerFilter()
        parts: list[str] = []
        async with aclosing(tutor.ask(prompt)) as events:
            async for ev in events:
                if isinstance(ev, Done):
                    if rest := marker.flush():
                        parts.append(rest)
                        yield TextDelta(rest)
                    doc = self.library.get(doc_id)   # có thể đã đổi tên trong lúc giảng
                    doc.session_id = ev.session_id or tutor.session_id
                    self.library.save(doc)
                    self.library.append_history(doc_id, question, "".join(parts), doc.part)
                    if on_doc:
                        await on_doc(doc)
                    yield ev
                    continue
                text = marker.push(ev.text)
                if marker.found:
                    k, n, title = marker.found
                    marker.found = None
                    doc = self.library.get(doc_id)
                    self.library.set_part(doc, k, n, title)
                    if on_doc:
                        await on_doc(doc)
                if text:
                    parts.append(text)
                    yield TextDelta(text)
