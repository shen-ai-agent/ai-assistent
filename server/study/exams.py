"""Dạng đề thi cho tab Học: mỗi kỳ thi một file `data/exams/<key>.md` mô tả format đề lấy từ nguồn chính thức.

Mỗi lớp học chọn một dạng đề (`Subject.exam`). Mọi bài trong lớp được nạp nguyên file đó vào system prompt của gia sư
(`ClaudeCliBridge.extra_prompt`) ở mỗi lượt, nên đổi dạng đề hay sửa file là lượt sau có hiệu lực ngay, kể cả bài
đang học dở. Quy tắc trả lời chung (đáp án kèm keyword, cách chấm) nằm trong prompts/study_reply.md.

Định dạng file: dòng `# Tên hiển thị` đầu tiên là tên, dòng `> ...` đầu tiên là mô tả ngắn hiện trên web,
phần còn lại là hướng dẫn ra đề cho gia sư. Thêm môn mới = thêm một file, không cần sửa code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


@dataclass
class ExamProfile:
    key: str        # tên file không đuôi, lưu trong subjects.json
    name: str
    summary: str
    text: str       # cả file, đưa vào system prompt

    def public(self) -> dict:
        return {"key": self.key, "name": self.name, "summary": self.summary}


def parse_profile(key: str, text: str) -> ExamProfile:
    name = summary = ""
    for line in text.splitlines():
        s = line.strip()
        if not name and s.startswith("# "):
            name = s[2:].strip()
        elif not summary and s.startswith("> "):
            summary = s[2:].strip()
        if name and summary:
            break
    return ExamProfile(key, name or key, summary, text.strip())


class ExamProfiles:
    """Đọc lại file mỗi lần gọi: vài file nhỏ, sửa là thấy ngay, không cần khởi động lại server."""

    def __init__(self, root: Path):
        self.root = root

    def all(self) -> list[ExamProfile]:
        if not self.root.is_dir():
            return []
        out = []
        for path in sorted(self.root.glob("*.md")):
            if _KEY.match(path.stem):
                out.append(parse_profile(path.stem, path.read_text(encoding="utf-8")))
        return out

    def get(self, key: str | None) -> ExamProfile | None:
        if not key or not _KEY.match(key):
            return None
        path = self.root / f"{key}.md"
        if not path.is_file():
            return None
        return parse_profile(key, path.read_text(encoding="utf-8"))
