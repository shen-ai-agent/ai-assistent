"""Nhận tên gọi trong transcript: "Mimi ơi", "hey Mimi", "ê Mimi, EC2 là gì".

So khớp gần đúng sau khi bỏ dấu, vì Whisper viết tên theo nhiều kiểu ("Mimi", "Mi Mi", "Mimmy", "Mimì").
Tên chỉ được tính khi đứng đầu câu, trước nó chỉ có từ gọi (hey, ê, này...): câu kể về Mimi giữa
cuộc nói chuyện không đánh thức trợ lý. Không đưa tên vào initial_prompt của Whisper: prompt làm model
"nghe thấy" tên cả khi không có (hermes-agent PR #116470).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

# Từ gọi đứng trước tên, đã bỏ dấu ("ê" → "e", "này" → "nay")
DEFAULT_PREFIXES = ("hey", "hay", "he", "hi", "hello", "e", "a", "o", "u", "oi", "alo", "lo", "nay", "ne",
                    "ok", "oke", "okay", "chao", "xin", "thua", "em", "ban", "co", "chi", "be")
# Từ đệm ngay sau tên, bỏ khỏi phần câu hỏi
DEFAULT_FILLERS = ("oi", "a", "ah", "ne", "nhe", "nha", "o", "e", "hey", "u", "uh", "um")

_WORD = re.compile(r"\w+")
_PUNCT = re.compile(r"[,.!?;:]")


def fold(word: str) -> str:
    """Bỏ dấu, chữ thường, gần đúng theo âm: y → i, chữ cái lặp liền nhau gộp một ("Mimmy" → "mimi")."""
    s = unicodedata.normalize("NFD", word.lower().replace("đ", "d"))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).replace("y", "i")
    return re.sub(r"(.)\1+", r"\1", s)


def _similarity(a: str, b: str) -> float:
    """Số ký tự khớp / độ dài chuỗi dài hơn. Khác ratio() của difflib (2M/(a+b)) ở chỗ chuỗi dài hơn tên
    bị trừ điểm đủ mạnh: "mai mấy" → "maimai" khớp 4 ký tự của "mimi", ratio() = 0.8, ở đây = 0.67."""
    m = sum(block.size for block in SequenceMatcher(None, a, b).get_matching_blocks())
    return m / max(len(a), len(b), 1)


@dataclass
class WakeMatch:
    found: bool
    rest: str = ""        # phần sau tên (giữ nguyên dấu), đã bỏ từ đệm và dấu câu đầu
    score: float = 0.0    # độ giống của tên tìm được (0..1)
    heard: str = ""       # cụm từ được nhận là tên

    @property
    def rest_words(self) -> int:
        return len(_WORD.findall(self.rest))


class WakeMatcher:
    def __init__(self, name: str = "Mimi", aliases: list[str] | tuple[str, ...] = (), *,
                 min_score: float = 0.8, max_prefix: int = 2,
                 prefixes: tuple[str, ...] = DEFAULT_PREFIXES, fillers: tuple[str, ...] = DEFAULT_FILLERS):
        # Mỗi biến thể so khớp ở dạng đã gộp (bỏ khoảng trắng): "mi mi" và "mimi" cùng là "mimi"
        self.variants = sorted({"".join(fold(w) for w in _WORD.findall(v)) for v in [name, *aliases]} - {""})
        self.min_score, self.max_prefix = min_score, max_prefix
        self.prefixes, self.fillers = {fold(w) for w in prefixes}, {fold(w) for w in fillers}
        self.max_words = max(len(_WORD.findall(v)) for v in [name, *aliases]) + 1  # tên có thể bị tách thêm một từ

    def _best(self, words: list[re.Match], folded: list[str]) -> tuple[float, int, int] | None:
        """(score, i, j) của cụm đầu câu giống tên nhất: tên là words[i:j], trước nó chỉ có từ gọi."""
        best: tuple[float, int, int] | None = None
        for i in range(min(self.max_prefix + 1, len(words))):
            if i and folded[i - 1] not in self.prefixes:
                break  # trước tên chỉ được có từ gọi
            for j in range(i + 1, min(i + self.max_words, len(words)) + 1):
                joined = "".join(folded[i:j])
                score = max(_similarity(joined, v) for v in self.variants)
                if best is None or score > best[0]:
                    best = (score, i, j)
        return best

    def _rest(self, text: str, words: list[re.Match], folded: list[str], j: int) -> str:
        """Phần câu sau words[:j], bỏ từ đệm ngay sau tên."""
        k = j
        while k < len(words) and folded[k] in self.fillers:
            k += 1
        return text[words[k].start():].strip() if k < len(words) else ""

    def match(self, text: str) -> WakeMatch:
        words = list(_WORD.finditer(text))
        folded = [fold(w.group()) for w in words]
        best = self._best(words, folded)
        if best is None or best[0] < self.min_score:
            return WakeMatch(False, score=best[0] if best else 0.0)
        score, i, j = best
        return WakeMatch(True, self._rest(text, words, folded, j), round(score, 3),
                         text[words[i].start():words[j - 1].end()])

    def strip_name(self, text: str, named: WakeMatch) -> str:
        """Bỏ tên ở đầu câu khi lượt dò tên đã nghe thấy tên (`named`, kết quả match() của lượt đó).

        Lượt nhận dạng chính (prompt glossary) có thể viết tên khác hẳn ("Mimi ơi. Mở giảm." thành
        "Nhiên mê ơi, mở diễn."), match() không nhận ra nên cả câu thành câu hỏi. Khi đó cắt sau dấu câu
        nằm ngay sau tên (trong số từ của tên + 2); không có thì bỏ cụm đầu câu giống tên nhất, dù điểm thấp.
        Câu bắt đầu đúng bằng từ đầu tiên sau tên của lượt dò tên thì lượt này không viết tên: giữ nguyên.
        """
        m = self.match(text)
        if m.found:
            return m.rest
        words = list(_WORD.finditer(text))
        folded = [fold(w.group()) for w in words]
        after = [fold(w) for w in _WORD.findall(named.rest)[:1]]
        if after and folded[:1] == after:
            return text.strip()
        n = max(len(_WORD.findall(named.heard)), 1)
        for k in range(min(n + 2, len(words) - 1)):
            if _PUNCT.search(text[words[k].end():words[k + 1].start()]):
                return text[words[k + 1].start():].strip()
        best = self._best(words, folded)
        return self._rest(text, words, folded, best[2]) if best else ""


def decide(m: WakeMatch, audio_ms: int, window_ms: int, min_rest_words: int) -> str:
    """Việc cần làm với một đoạn nói lúc SLEEP: "ask" (tên kèm câu hỏi), "ack" (chỉ gọi tên) hay "ignore".

    Chỉ nhận dạng window_ms đầu đoạn để tìm tên, nên đoạn dài hơn cửa sổ chắc chắn còn câu hỏi phía sau.
    """
    if not m.found:
        return "ignore"
    return "ask" if m.rest_words >= min_rest_words or audio_ms > window_ms else "ack"
