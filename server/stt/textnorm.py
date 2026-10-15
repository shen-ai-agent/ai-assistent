"""Chuẩn hoá text trước khi so sánh transcript (WER, term recall).

Chỉ dùng để chấm điểm, không dùng để hiển thị. Hai phía (đáp án và kết quả STT)
luôn đi qua cùng một hàm để cách viết khác nhau không bị tính là lỗi:
".NET" / "dotnet" / "dot net", "C#" / "c sharp", "X-Ray" / "x ray".
Khác biệt chỉ do khoảng trắng ("EC 2" / "EC2", "Private Link" / "PrivateLink")
được bỏ qua khi tìm thuật ngữ (contains_term), nhưng vẫn tính vào WER.
"""

from __future__ import annotations

import re
import unicodedata

# Ký hiệu phải đổi thành chữ trước khi xoá dấu câu, nếu không sẽ mất nghĩa.
_SYMBOLS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"asp\s*\.\s*net", re.I), "asp dotnet"),
    (re.compile(r"(?<![\w])\.\s*net\b", re.I), " dotnet"),
    (re.compile(r"\bdot\s+net\b", re.I), "dotnet"),
    (re.compile(r"\bc\s*#", re.I), "c sharp"),
    (re.compile(r"\bc\s+sharp\b", re.I), "c sharp"),
]
_SEPARATORS = re.compile(r"[-/_]")
_NON_WORD = re.compile(r"[^\w\s]")
# Whisper viết số bằng chữ số; đáp án có thể viết bằng chữ ("since three years").
# Chỉ đổi số đếm tiếng Anh: số tiếng Việt ("ba", "năm") trùng với từ thường nên giữ nguyên.
_EN_NUMBERS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty".split())}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    for pattern, repl in _SYMBOLS:
        text = pattern.sub(repl, text)
    text = text.lower()
    text = _SEPARATORS.sub(" ", text)
    text = _NON_WORD.sub("", text)
    return " ".join(_EN_NUMBERS.get(w, w) for w in text.split())


def contains_term(normalized_text: str, term: str) -> bool:
    """Thuật ngữ có xuất hiện trong text đã chuẩn hoá không.

    So theo nguyên token và bỏ qua khoảng trắng: một dãy token liên tiếp, ghép lại
    không có dấu cách, phải trùng với thuật ngữ cũng đã bỏ dấu cách.
    "S" không khớp "s3", nhưng "cicd" khớp "CI/CD".
    """
    target = normalize(term).replace(" ", "")
    if not target:
        return False
    tokens = normalized_text.split()
    for i in range(len(tokens)):
        joined = ""
        for tok in tokens[i:]:
            joined += tok
            if joined == target:
                return True
            if len(joined) >= len(target) or not target.startswith(joined):
                break
    return False
