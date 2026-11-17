"""Đo nhịp nói từ mốc thời gian từng từ (server/tts/prosody.py)."""

import pytest

from server.tts.prosody import Word, boundary_kind, measure, merge_gaps, suggest_pauses


def words_from(spec: list[tuple[str, float]], dur: float = 0.2) -> list[Word]:
    """spec: (từ, khoảng nghỉ sau từ). Mỗi từ dài dur giây."""
    out, t = [], 0.0
    for text, gap in spec:
        out.append(Word(text, t, t + dur))
        t += dur + gap
    return out


def test_boundary_kind():
    assert boundary_kind(" chủ,") == "comma" and boundary_kind(" chủ.") == "sentence"
    assert boundary_kind(" gì?") == "question" and boundary_kind(" là:") == "colon"
    assert boundary_kind(' "đó."') == "sentence" and boundary_kind(" Lambda") == "none"


def test_measure_gaps_by_punctuation():
    ws = words_from([("Lambda", 0.02), ("chạy,", 0.2), ("không", 0.02), ("cần", 0.02), ("chủ.", 0.6),
                     ("Vì", 0.02), ("sao?", 0.9), ("Vì", 0.4), ("rẻ.", 0.0)])
    st = measure(ws)
    g = st["gaps"]
    assert g["comma"]["median"] == 200 and g["sentence"]["median"] == 600 and g["question"]["median"] == 900
    assert st["unpunct_pauses"][0]["ms"] == 400 and "Vì | rẻ." in st["unpunct_pauses"][0]["at"]
    assert st["articulation_rate"] == pytest.approx(5.0, rel=0.25)


def test_merge_and_suggest():
    a = words_from([("một.", 0.5), ("hai.", 0.7), ("ba", 0.0)])
    b = words_from([("bốn.", 0.6), ("năm?", 0.8), ("sáu", 0.0)])
    gaps = merge_gaps([a, b])
    assert gaps["sentence"]["n"] == 3 and gaps["sentence"]["median"] == 600
    sug = suggest_pauses(gaps, native_ms=180)
    assert sug["sentence"] == 420 and sug["question"] == 620 and sug["comma"] == 0 and sug["colon"] is None
