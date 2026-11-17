"""Đo chất giọng từng câu để biết giọng TTS có giữ đều giữa các câu không (bench/tts_consistency.py).

ZeroTTS sinh mỗi câu độc lập, lấy mẫu ngẫu nhiên, nên cao độ, độ to và độ "hơi" có thể trôi từ câu này sang câu
khác. Người nghe nhận ra nhất là chỗ lệch giữa hai câu kề nhau, nên ngoài độ lệch chuẩn còn tính độ nhảy trung bình
giữa hai câu liền nhau.

Chỉ dùng numpy/scipy, không thêm package:
  f0_st        trung vị cao độ, đơn vị bán cung so với 100Hz (lệch 1 bán cung là nghe rõ)
  f0_range_st  biên độ cao độ trong câu (p90 - p10); quá hẹp là đều đều, quá rộng là lên xuống thất thường
  loud_db      độ to trung bình các frame có tiếng
  tilt_db      năng lượng 0-1kHz trừ 1-4kHz; giọng hơi/thều thào thì nhỏ đi, giọng chắc thì lớn
  periodicity  độ tuần hoàn (đỉnh tự tương quan) trung vị; lẫn nhiều hơi hay khàn thì thấp
"""

from __future__ import annotations

import numpy as np

SR = 16000
FRAME = 640          # 40ms, đủ chứa 2 chu kỳ của giọng trầm nhất cần đo
HOP = 160            # 10ms
F0_MIN, F0_MAX = 75.0, 500.0
VOICED_R = 0.5       # đỉnh tự tương quan từ chừng này trở lên thì coi là có thanh
SILENCE_DB = 35.0    # frame nhỏ hơn frame to nhất chừng này dB thì coi là lặng
KEYS = ("f0_st", "f0_range_st", "loud_db", "tilt_db", "periodicity")


def _to16k(audio: np.ndarray, sr: int) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float64).reshape(-1)
    if sr == SR:
        return audio
    from math import gcd
    from scipy.signal import resample_poly
    g = gcd(SR, sr)
    return resample_poly(audio, SR // g, sr // g)


def frame_features(audio: np.ndarray, sr: int) -> dict[str, np.ndarray]:
    """Đặc trưng từng frame 10ms: rms_db, f0 (Hz, 0 nếu không thanh), r (độ tuần hoàn), tilt_db."""
    x = _to16k(audio, sr)
    if len(x) < FRAME:
        x = np.pad(x, (0, FRAME - len(x)))
    n = 1 + (len(x) - FRAME) // HOP
    idx = np.arange(FRAME)[None, :] + HOP * np.arange(n)[:, None]
    win = np.hanning(FRAME)
    fr = (x[idx] - x[idx].mean(axis=1, keepdims=True)) * win

    rms_db = 10 * np.log10(np.mean(fr ** 2, axis=1) + 1e-12)

    # Tự tương quan qua FFT, chia cho tự tương quan của cửa sổ để bỏ độ dốc do cửa sổ (Boersma 1993)
    nfft = 2 * FRAME
    spec = np.fft.rfft(fr, nfft)
    ac = np.fft.irfft(np.abs(spec) ** 2, nfft)[:, :FRAME]
    wac = np.fft.irfft(np.abs(np.fft.rfft(win, nfft)) ** 2, nfft)[:FRAME]
    ac = ac / (ac[:, :1] + 1e-12) / (wac / wac[0] + 1e-12)
    lo, hi = int(SR / F0_MAX), int(SR / F0_MIN)
    seg = ac[:, lo:hi]
    # Âm tuần hoàn có đỉnh ở mọi bội số chu kỳ (T, 2T...) gần bằng nhau: lấy đỉnh cục bộ có độ trễ ngắn nhất
    # mà đạt >= 90% đỉnh cao nhất, nếu không sẽ lệch quãng tám xuống
    peak = np.zeros_like(seg, dtype=bool)
    peak[:, 1:-1] = (seg[:, 1:-1] >= seg[:, :-2]) & (seg[:, 1:-1] >= seg[:, 2:])
    good = peak & (seg >= 0.9 * seg.max(axis=1, keepdims=True))
    k = np.where(good.any(axis=1), np.argmax(good, axis=1), np.argmax(seg, axis=1))
    r = seg[np.arange(n), k]
    # Nội suy parabol quanh đỉnh cho F0 mịn hơn 1 mẫu
    km, kp = np.clip(k - 1, 0, seg.shape[1] - 1), np.clip(k + 1, 0, seg.shape[1] - 1)
    a, b, c = seg[np.arange(n), km], r, seg[np.arange(n), kp]
    den = a - 2 * b + c
    off = np.where(np.abs(den) > 1e-9, 0.5 * (a - c) / np.where(den == 0, 1, den), 0.0)
    lag = lo + k + np.clip(off, -1, 1)

    freqs = np.fft.rfftfreq(nfft, 1 / SR)
    pw = np.abs(spec) ** 2
    low = pw[:, (freqs >= 50) & (freqs < 1000)].sum(axis=1)
    high = pw[:, (freqs >= 1000) & (freqs < 4000)].sum(axis=1)
    tilt_db = 10 * np.log10((low + 1e-12) / (high + 1e-12))

    loud = rms_db > rms_db.max() - SILENCE_DB
    voiced = loud & (r >= VOICED_R) & (k > 0) & (k < seg.shape[1] - 1)
    f0 = np.where(voiced, SR / lag, 0.0)
    return {"rms_db": rms_db, "f0": f0, "r": r, "tilt_db": tilt_db, "loud": loud}


def voice_features(audio: np.ndarray, sr: int) -> dict:
    """Tóm tắt một câu. Câu quá ngắn / không có thanh thì các giá trị cao độ là nan."""
    ff = frame_features(audio, sr)
    voiced = ff["f0"] > 0
    st = 12 * np.log2(ff["f0"][voiced] / 100.0) if voiced.any() else np.array([])
    nan = float("nan")
    return {
        "f0_st": float(np.median(st)) if st.size >= 5 else nan,
        "f0_range_st": float(np.percentile(st, 90) - np.percentile(st, 10)) if st.size >= 5 else nan,
        "loud_db": float(np.mean(ff["rms_db"][ff["loud"]])) if ff["loud"].any() else nan,
        "tilt_db": float(np.median(ff["tilt_db"][voiced])) if voiced.any() else nan,
        "periodicity": float(np.median(ff["r"][voiced])) if voiced.any() else nan,
        "voiced_frac": float(voiced.sum() / max(1, ff["loud"].sum())),
    }


def spread(feats: list[dict]) -> dict:
    """Độ lệch giữa các câu: std và độ nhảy trung bình giữa hai câu kề nhau (|câu i+1 - câu i|)."""
    out = {}
    for key in KEYS:
        v = np.array([f[key] for f in feats], dtype=float)
        v = v[~np.isnan(v)]
        out[key] = {
            "mean": round(float(v.mean()), 2) if v.size else None,
            "std": round(float(v.std()), 2) if v.size > 1 else None,
            "jump": round(float(np.abs(np.diff(v)).mean()), 2) if v.size > 1 else None,
        }
    return out
