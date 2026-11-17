"""Từ cuối câu có bị nuốt không, và kết câu kiểu nào thì model đọc rõ từ cuối.

Cùng một câu, cùng seed, đổi dấu kết câu (giữ `.`, bỏ dấu, đổi thành `,`...) rồi cho Whisper (CPU, word timestamps)
nghe lại. Mỗi bản đo:
  - Whisper có nghe ra từ cuối không, và độ chắc (probability) của từ cuối so với các từ khác trong câu;
  - độ dài từ cuối, độ to 80ms cuối có tiếng so với cả câu (nhỏ hẳn = đọc lí nhí rồi tắt).

    .venv/Scripts/python bench/tts_tail.py --session _local/sessions/2026-11-17.jsonl
    .venv/Scripts/python bench/tts_tail.py --session ... --variants dot,bare,comma --cfg 1.5 --seeds 2
    .venv/Scripts/python bench/tts_tail.py --session ... --variants dot --text-temps 1.0,0.7,0.4
    .venv/Scripts/python bench/tts_tail.py --compare

Mặc định chạy CPU để không tranh GPU với server. Kết quả: _local/tts_tail/results.json và wav để nghe.

Đã đo (17/11/2026): ZeroTTS không cắt ngang âm cuối. Frame trước <eoa> đã tắt dần về im lặng, frame <eoa> im hẳn;
ép giữ thêm frame sau <eoa> (`eoa_extra_frames` > 1) chỉ sinh ra tạp âm.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.tts.synth import TtsService, ZeroTtsEngine  # noqa: E402

OUT = ROOT / "_local" / "tts_tail"
RESULTS = OUT / "results.json"
_END = re.compile(r"[.!?…]+$")

# Cách đổi dấu kết câu: câu đã bỏ dấu cuối → text đưa vào model
VARIANTS = {
    "dot": lambda s: s + ".",
    "bare": lambda s: s,
    "comma": lambda s: s + ",",
    "dots": lambda s: s + "...",
    "semi": lambda s: s + ";",
}


def answers(session: Path, n: int) -> list[str]:
    rows = [json.loads(x) for x in session.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r["answer"] for r in rows if r.get("answer") and not r.get("error")][-n:]


def fold(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").replace("đ", "d")
    return "".join(c if c.isalnum() else " " for c in s)


def trail_s(audio: np.ndarray, sr: int) -> float:
    """Khoảng lặng sau tiếng cuối (giây)."""
    w = int(sr * 0.01)
    n = len(audio) // w
    rms = np.sqrt(np.mean(audio[: n * w].reshape(n, w) ** 2, axis=1) + 1e-12)
    voiced = np.nonzero(rms > rms.max() * 0.03)[0]
    return round((n - 1 - voiced[-1]) * 0.01, 2) if len(voiced) else 0.0


def end_db(audio: np.ndarray, sr: int) -> float:
    """dB của 80ms cuối còn có tiếng so với trung vị các khung có tiếng (âm = từ cuối nhỏ hơn phần còn lại)."""
    w = int(sr * 0.04)
    n = len(audio) // w
    rms = np.sqrt(np.mean(audio[: n * w].reshape(n, w) ** 2, axis=1) + 1e-12)
    voiced = np.nonzero(rms > rms.max() * 0.05)[0]
    if len(voiced) < 4:
        return 0.0
    ref = np.median(rms[voiced])
    last = rms[voiced[-1] - 1: voiced[-1] + 1]
    return float(20 * np.log10(np.mean(last) / ref))


def to16k(audio: np.ndarray, sr: int) -> np.ndarray:
    from scipy.signal import resample_poly
    g = np.gcd(sr, 16000)
    return resample_poly(audio, 16000 // g, sr // g).astype(np.float32)


def score(whisper, audio: np.ndarray, sr: int, text: str, source: str) -> dict:
    segs, _ = whisper.transcribe(to16k(audio, sr), language="vi", beam_size=5, word_timestamps=True,
                                 condition_on_previous_text=False, vad_filter=False)
    words = [w for s in segs for w in (s.words or [])]
    hyp = "".join(w.word for w in words).strip()
    # Từ cuối có thể là thuật ngữ đã đổi sang cách đọc ("my ét quy eo"): Whisper viết lại thành "MySQL"
    want = {fold(text).split()[-1], *fold(source).split()[-1:]}
    out = {"hyp": hyp, "last_ok": False, "last_prob": 0.0, "mid_prob": None, "last_dur": None}
    if not words:
        return out
    # Whisper có thể gộp/tách từ: tìm từ cuối mong muốn trong 2 từ cuối nghe được
    tail = [(i, w) for i, w in list(enumerate(words))[-2:] if want & set(fold(w.word).split())]
    if tail:
        i, w = tail[-1]
        out.update(last_ok=True, last_prob=round(w.probability, 3), last_dur=round(w.end - w.start, 3))
    rest = [w.probability for w in words[:-1]]
    out["mid_prob"] = round(float(np.mean(rest)), 3) if rest else None
    return out


def print_table(rows: list[dict]) -> None:
    print("\nTừ cuối câu theo kiểu kết câu (trung bình mọi câu × seed):")
    print(f"  {'cfg':>4s} {'ttemp':>5s} {'kiểu':6s} {'nghe ra từ cuối':>16s} {'prob từ cuối':>13s} {'prob từ khác':>13s}"
          f" {'dài từ cuối':>12s} {'to cuối dB':>11s} {'lặng cuối':>10s}")
    keys = sorted({(r["cfg"], r.get("tt", 1.0), r["variant"]) for r in rows},
                  key=lambda k: (k[0], -k[1], list(VARIANTS).index(k[2])))
    for cfg, tt, v in keys:
        rs = [r for r in rows if r["cfg"] == cfg and r.get("tt", 1.0) == tt and r["variant"] == v]
        ok = [r["last_ok"] for r in rs]
        mid = [r["mid_prob"] for r in rs if r["mid_prob"] is not None]
        dur = [r["last_dur"] for r in rs if r["last_dur"] is not None]
        print(f"  {cfg:4.1f} {tt:5.1f} {v:6s} {np.mean(ok) * 100:7.0f}% ({sum(ok):2d}/{len(ok):2d})"
              f" {np.mean([r['last_prob'] for r in rs]):13.3f} {np.mean(mid):13.3f}"
              f" {np.mean(dur) if dur else 0:11.2f}s {np.median([r['end_db'] for r in rs]):11.1f}"
              f" {np.mean([r.get('trail', 0) for r in rs]):9.2f}s")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", type=Path)
    ap.add_argument("--answers", type=int, default=3, help="lấy N câu trả lời cuối")
    ap.add_argument("--sentences", type=int, default=16, help="tối đa N câu kết bằng dấu chấm")
    ap.add_argument("--variants", default="dot,bare,comma,dots")
    ap.add_argument("--cfg", default="1.5")
    ap.add_argument("--seeds", type=int, default=2)
    ap.add_argument("--temperature", type=float, default=0.8)
    ap.add_argument("--text-temps", default="1.0", help="text_temperature: lấy mẫu kênh điều khiển (có <eoa> hay chưa)")
    ap.add_argument("--compare", action="store_true", help="in lại bảng từ results.json")
    args = ap.parse_args()

    if args.compare:
        print_table(json.loads(RESULTS.read_text(encoding="utf-8")))
        return
    if not args.session:
        ap.error("cần --session")

    engine = ZeroTtsEngine("maichi", "cpu", 4, temperature=args.temperature)
    svc = TtsService(engine)
    sr = svc.sample_rate
    base = [(s.text[:-1], s.source) for a in answers(args.session, args.answers) for s in svc.plan(a)
            if s.lang == "vi" and s.text.endswith(".") and not s.text.endswith("..")][: args.sentences]
    OUT.mkdir(parents=True, exist_ok=True)
    import soundfile as sf
    from faster_whisper import WhisperModel
    whisper = WhisperModel("large-v3", device="cpu", compute_type="int8",
                           download_root=str(ROOT / "models" / "hf"), local_files_only=True)

    rows = []
    for cfg, tt in [(float(c), float(t)) for c in args.cfg.split(",") for t in args.text_temps.split(",")]:
        engine.sampling = dict(audio_temperature=args.temperature, cfg_scale=cfg, text_temperature=tt)
        for seed in range(args.seeds):
            for i, (body, source) in enumerate(base):
                for v in args.variants.split(","):
                    text = VARIANTS[v](body)
                    np.random.seed(1000 * seed + i)
                    audio = np.concatenate(list(engine.stream(text, "vi")))
                    sf.write(str(OUT / f"c{cfg}_tt{tt}_s{seed}_{i:02d}_{v}.wav"), audio, sr, subtype="PCM_16")
                    row = {"cfg": cfg, "tt": tt, "seed": seed, "i": i, "variant": v, "text": text,
                           "sec": round(len(audio) / sr, 2), "end_db": round(end_db(audio, sr), 1),
                           "trail": trail_s(audio, sr),
                           **score(whisper, audio, sr, text, source)}
                    rows.append(row)
                    print(f"c{cfg} tt{tt} s{seed} #{i} {v:5s} {'ok ' if row['last_ok'] else 'SAI'} "
                          f"p={row['last_prob']:.2f} → {row['hyp'][-40:]}", flush=True)
                RESULTS.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    print_table(rows)


if __name__ == "__main__":
    main()
