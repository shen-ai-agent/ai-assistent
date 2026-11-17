"""Đo nhịp nói từ mốc thời gian từng từ (Whisper word_timestamps): nghỉ bao lâu sau dấu phẩy, dấu chấm, câu hỏi,
nói nhanh bao nhiêu âm tiết/giây, ngừng ở đâu khi không có dấu câu (thường là trước ý muốn nhấn).

Dùng để chỉnh normalize.PAUSES theo số đo bài giảng thật (bench/lecture_prosody.py), và đo giọng TTS bằng cùng
thước đo (bench/tts_render.py rồi đo file wav) để so cho công bằng: Whisper hay kéo dài mốc kết thúc từ vào khoảng
lặng, nên số tuyệt đối lệch, nhưng hai bên lệch như nhau.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

KINDS = ("none", "comma", "colon", "sentence", "question")
RUN_GAP_S = 0.25      # nghỉ từ chừng này trở lên thì tính là một lần ngừng, tách hai "hơi nói"
LONG_GAP_S = 0.7      # ngừng dài: thường là chuyển ý


@dataclass
class Word:
    text: str
    start: float
    end: float


def boundary_kind(word: str) -> str:
    """Loại ranh giới ngay sau từ này, theo dấu câu dính ở cuối từ."""
    w = word.strip().rstrip("\"'”’)»")
    if not w:
        return "none"
    return {",": "comma", ";": "comma", ":": "colon", "?": "question",
            ".": "sentence", "!": "sentence", "…": "sentence"}.get(w[-1], "none")


def _q(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _summary(xs: list[float]) -> dict:
    ms = [x * 1000 for x in xs]
    return {"n": len(ms), "p25": round(_q(ms, 0.25)), "median": round(_q(ms, 0.5)), "p75": round(_q(ms, 0.75)),
            "p90": round(_q(ms, 0.9))} if ms else {"n": 0}


def measure(words: list[Word], context: int = 4) -> dict:
    """Thống kê nhịp từ danh sách từ (theo thứ tự thời gian). Tiếng Việt: mỗi từ Whisper ≈ một âm tiết."""
    gaps: dict[str, list[float]] = {k: [] for k in KINDS}
    unpunct: list[tuple[float, str]] = []  # ngừng không có dấu câu: (độ dài, ngữ cảnh)
    for i in range(len(words) - 1):
        gap = max(0.0, words[i + 1].start - words[i].end)
        kind = boundary_kind(words[i].text)
        gaps[kind].append(gap)
        if kind == "none" and gap >= RUN_GAP_S:
            before = " ".join(w.text.strip() for w in words[max(0, i - context + 1): i + 1])
            after = " ".join(w.text.strip() for w in words[i + 1: i + 1 + context])
            unpunct.append((gap, f"{before} | {after}"))

    # Tốc độ: âm tiết/giây trong từng hơi nói (bỏ các lần ngừng), và tính cả ngừng
    runs, start = [], 0
    for i in range(1, len(words) + 1):
        if i == len(words) or words[i].start - words[i - 1].end >= RUN_GAP_S:
            runs.append((i - start, words[i - 1].end - words[start].start))
            start = i
    speak_s = sum(d for _, d in runs)
    total_s = words[-1].end - words[0].start if words else 0.0

    # Kéo dài trước ranh giới: từ cuối câu dài hơn từ thường bao nhiêu lần
    dur = [w.end - w.start for w in words]
    final = [d for w, d in zip(words, dur) if boundary_kind(w.text) in ("sentence", "question")]
    inner = [d for w, d in zip(words, dur) if boundary_kind(w.text) == "none"]
    lengthening = statistics.median(final) / statistics.median(inner) if final and inner else float("nan")

    all_bound = gaps["sentence"] + gaps["question"]
    return {
        "words": len(words),
        "minutes": round(total_s / 60, 2),
        "gaps": {k: _summary(v) for k, v in gaps.items()},
        "articulation_rate": round(len(words) / speak_s, 2) if speak_s else None,   # âm tiết/giây khi đang nói
        "speech_rate": round(len(words) / total_s, 2) if total_s else None,          # tính cả ngừng
        "long_pauses_per_min": round(sum(g >= LONG_GAP_S for g in all_bound) / (total_s / 60), 2) if total_s else None,
        "boundary_lengthening": round(lengthening, 2),
        "unpunct_pauses": [{"ms": round(g * 1000), "at": c} for g, c in sorted(unpunct, reverse=True)],
    }


def merge_gaps(words: list[list[Word]]) -> dict:
    """Gộp nhiều file: đo lại trên toàn bộ khoảng nghỉ, không lấy trung bình các trung vị."""
    gaps: dict[str, list[float]] = {k: [] for k in KINDS}
    for ws in words:
        for i in range(len(ws) - 1):
            gaps[boundary_kind(ws[i].text)].append(max(0.0, ws[i + 1].start - ws[i].end))
    return {k: _summary(v) for k, v in gaps.items()}


def suggest_pauses(gaps: dict, native_ms: int = 180) -> dict:
    """Đổi khoảng nghỉ đo được thành normalize.PAUSES: trừ phần lặng sẵn có ở hai đầu câu ZeroTTS (~180ms).

    para lấy p90 của nghỉ sau câu (Whisper không biết chỗ chuyển đoạn, nhưng nghỉ dài nhất thường là chuyển ý).
    """
    def ins(kind: str, key: str = "median") -> int | None:
        g = gaps.get(kind, {})
        return max(0, g[key] - native_ms) if g.get("n") else None

    return {"comma": 0, "sentence": ins("sentence"), "question": ins("question"),
            "colon": ins("colon"), "para": ins("sentence", "p90")}
