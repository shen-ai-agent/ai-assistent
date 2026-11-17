"""Giọng TTS có giữ đều giữa các câu không: đọc cùng một câu trả lời với nhiều cấu hình lấy mẫu, đo chất giọng từng
câu (server/tts/voicefeat.py) và xuất wav để nghe.

    .venv/Scripts/python bench/tts_consistency.py --session _local/sessions/2026-11-17.jsonl
    .venv/Scripts/python bench/tts_consistency.py --session ... --configs t0.8,t0.5,t0.8c1.5 --gpu
    .venv/Scripts/python bench/tts_consistency.py --compare

Cấu hình viết dạng t<temperature>[c<cfg_scale>], ví dụ t0.6c1.5. Mặc định chạy CPU để không tranh GPU với server.
Số liệu: độ nhảy trung bình giữa hai câu kề nhau (người nghe nhận ra cái này nhất) và độ lệch chuẩn cả bài.
Kết quả: _local/tts_consistency/<cấu hình>_s<seed>.wav và results.json.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.tts.synth import TtsService, ZeroTtsEngine  # noqa: E402
from server.tts.voicefeat import KEYS, spread, voice_features  # noqa: E402

OUT = ROOT / "_local" / "tts_consistency"
RESULTS = OUT / "results.json"
NAMES = {"f0_st": "cao độ (bán cung)", "f0_range_st": "biên độ cao độ", "loud_db": "độ to (dB)",
         "tilt_db": "độ nghiêng phổ (dB)", "periodicity": "độ tuần hoàn"}


def parse_config(s: str) -> dict:
    m = re.fullmatch(r"t([\d.]+)(?:c([\d.]+))?", s)
    if not m:
        raise SystemExit(f"cấu hình sai: {s} (dạng t0.6 hoặc t0.6c1.5)")
    return {"audio_temperature": float(m[1]), "cfg_scale": float(m[2] or 1.0)}


def pick_text(session: Path, ts: str | None) -> tuple[str, str]:
    rows = [json.loads(x) for x in session.read_text(encoding="utf-8").splitlines() if x.strip()]
    rows = [r for r in rows if r.get("answer") and not r.get("error")]
    if ts:
        rows = [r for r in rows if ts in r["ts"]]
    if not rows:
        raise SystemExit("không tìm thấy câu trả lời")
    r = max(rows[-10:], key=lambda r: len(r["answer"]))
    return r["ts"], r["answer"]


def run_one(svc: TtsService, segs: list, seed: int) -> tuple[list[np.ndarray], float]:
    np.random.seed(seed)   # ZeroTTS lấy số ngẫu nhiên từ np.random toàn cục
    t0, audios = time.perf_counter(), []
    for seg in segs:
        audios.append(np.concatenate(list(svc.stream(seg))))
    return audios, time.perf_counter() - t0


def summarize(runs: list[dict]) -> dict:
    """Trung bình qua các seed."""
    out = {}
    for key in KEYS:
        out[key] = {s: round(float(np.mean([r["spread"][key][s] for r in runs if r["spread"][key][s] is not None])), 2)
                    for s in ("mean", "std", "jump")}
    out["rtf"] = round(float(np.mean([r["rtf"] for r in runs])), 2)
    out["sec"] = round(float(np.mean([r["sec"] for r in runs])), 1)
    return out


def print_table(results: dict) -> None:
    configs = list(results["configs"])
    print(f"\nVăn bản: {results['source']} ({results['sentences']} câu), seed {results['seeds']}, {results['device']}")
    print("Độ nhảy giữa hai câu kề nhau (nhỏ = đều giọng hơn) / std cả bài / trung bình:")
    print(f"  {'':22s}" + "".join(f"{c:>20s}" for c in configs))
    for key in KEYS:
        cells = []
        for c in configs:
            s = results["configs"][c]["summary"][key]
            cells.append(f"{s['jump']:6.2f} /{s['std']:5.2f} /{s['mean']:6.1f}")
        print(f"  {NAMES[key]:22s}" + "".join(f"{x:>20s}" for x in cells))
    print(f"  {'thời lượng (s)':22s}" + "".join(f"{results['configs'][c]['summary']['sec']:>20.1f}" for c in configs))
    print(f"  {'RTF':22s}" + "".join(f"{results['configs'][c]['summary']['rtf']:>20.2f}" for c in configs))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", type=Path, help="file jsonl trong _local/sessions")
    ap.add_argument("--ts", help="chọn câu trả lời theo một phần mốc thời gian, vd 17:40:33")
    ap.add_argument("--sentences", type=int, default=16, help="lấy N câu đầu")
    ap.add_argument("--configs", default="t0.8,t0.6,t0.5,t0.8c1.5,t0.6c1.5")
    ap.add_argument("--seeds", type=int, default=2)
    ap.add_argument("--voice", default="maichi")
    ap.add_argument("--gpu", action="store_true")
    ap.add_argument("--compare", action="store_true", help="in lại bảng từ results.json")
    args = ap.parse_args()

    if args.compare:
        print_table(json.loads(RESULTS.read_text(encoding="utf-8")))
        return
    if not args.session:
        ap.error("cần --session")

    ts, text = pick_text(args.session, args.ts)
    engine = ZeroTtsEngine(args.voice, "cuda" if args.gpu else "cpu", 4)
    svc = TtsService(engine)
    sr = svc.sample_rate
    segs = svc.plan(text)[:args.sentences]
    OUT.mkdir(parents=True, exist_ok=True)
    results = {"source": f"{args.session.name} {ts}", "sentences": len(segs), "seeds": args.seeds,
               "device": engine.provider, "texts": [s.source for s in segs], "configs": {}}

    for name in args.configs.split(","):
        engine.sampling = parse_config(name)
        runs = []
        for seed in range(args.seeds):
            audios, took = run_one(svc, segs, seed)
            feats = [voice_features(a, sr) for a in audios]
            sec = sum(len(a) for a in audios) / sr
            runs.append({"seed": seed, "rtf": round(took / sec, 2), "sec": round(sec, 1), "spread": spread(feats),
                         "per_sentence": [{k: round(v, 2) for k, v in f.items()} for f in feats]})
            parts = []
            for i, (a, seg) in enumerate(zip(audios, segs)):
                if i:
                    parts.append(np.zeros(sr * segs[i - 1].pause_ms // 1000, np.float32))
                parts.append(a)
            import soundfile as sf
            sf.write(str(OUT / f"{name}_s{seed}.wav"), np.concatenate(parts), sr, subtype="PCM_16")
            print(f"{name} seed {seed}: {sec:.1f}s audio, RTF {took / sec:.2f}", flush=True)
        results["configs"][name] = {"sampling": engine.sampling, "runs": runs, "summary": summarize(runs)}
        RESULTS.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")

    print_table(results)


if __name__ == "__main__":
    main()
