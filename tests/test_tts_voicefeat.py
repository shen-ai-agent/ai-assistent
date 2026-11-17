"""Đo chất giọng từng câu (server/tts/voicefeat.py) trên tín hiệu tổng hợp."""

import numpy as np
import pytest

from server.tts.voicefeat import spread, voice_features

SR = 48000


def voiced(f0: float, sec: float = 1.0, amp: float = 0.3, noise: float = 0.0, seed: int = 0) -> np.ndarray:
    """Nguyên âm giả: hoạ âm giảm dần theo bậc, có thể trộn nhiễu như giọng lẫn hơi."""
    t = np.arange(int(SR * sec)) / SR
    x = sum(np.sin(2 * np.pi * f0 * h * t) / h for h in range(1, 12))
    x = amp * x / np.abs(x).max()
    return x + noise * np.random.default_rng(seed).standard_normal(len(x))


def test_pitch_in_semitones():
    f = voice_features(voiced(200.0), SR)
    assert f["f0_st"] == pytest.approx(12.0, abs=0.2)          # 200Hz = 12 bán cung trên 100Hz
    assert f["f0_range_st"] < 0.3 and f["periodicity"] > 0.9


def test_breathy_and_quiet():
    clean, breathy = voice_features(voiced(220.0), SR), voice_features(voiced(220.0, noise=0.08), SR)
    assert breathy["periodicity"] < clean["periodicity"] and breathy["tilt_db"] < clean["tilt_db"]
    quiet = voice_features(voiced(220.0, amp=0.1), SR)
    assert clean["loud_db"] - quiet["loud_db"] == pytest.approx(20 * np.log10(3), abs=0.3)


def test_spread_jump_between_neighbours():
    feats = [voice_features(voiced(f), SR) for f in (200.0, 200.0 * 2 ** (2 / 12), 200.0)]
    s = spread(feats)
    assert s["f0_st"]["jump"] == pytest.approx(2.0, abs=0.2) and s["f0_st"]["std"] == pytest.approx(0.94, abs=0.1)
