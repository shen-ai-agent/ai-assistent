"""Thuật ngữ tiếng Anh đứng cuối câu (ngay trước dấu chấm) có bị đọc sai/nuốt hơn khi đứng giữa câu không.

Cùng thuật ngữ, cùng seed, đặt vào nhiều khung câu (cuối câu, cuối câu + "nhé", giữa câu), đọc bằng ZeroTTS (CPU) rồi
cho Whisper (CPU) nghe lại: có nghe ra đúng thuật ngữ không.

    .venv/Scripts/python bench/tts_term_position.py
    .venv/Scripts/python bench/tts_term_position.py --seeds 3 --terms "Neptune,Keyspaces"

Kết quả: _local/tts_term_position/results.json và wav để nghe.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.tts_tail import fold, to16k  # noqa: E402
from server.tts.synth import TtsService, ZeroTtsEngine  # noqa: E402

OUT = ROOT / "_local" / "tts_term_position"

# Thuật ngữ đứng cuối câu trong câu trả lời thật (_local/sessions), cả bản bị đọc sai lẫn bản đọc đúng
TERMS = ["Neptune", "Keyspaces", "Memcached", "ElastiCache", "Timestream", "DocumentDB", "Redshift", "Aurora",
         "Spectrum", "QLDB", "Multi-AZ", "DynamoDB"]
FRAMES = {
    "end": "Trường hợp này thì chọn {}.",
    "end_nhe": "Trường hợp này thì chọn {} nhé.",
    "mid": "Trường hợp này chọn {} là hợp lý nhất.",
}


def has_term(hyp: str, term: str) -> bool:
    return fold(term).replace(" ", "") in fold(hyp).replace(" ", "")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--terms", help="danh sách cách nhau dấu phẩy (mặc định TERMS)")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--cfg", type=float, default=1.5)
    args = ap.parse_args()
    terms = args.terms.split(",") if args.terms else TERMS

    engine = ZeroTtsEngine("maichi", "cpu", 4, cfg_scale=args.cfg)
    svc = TtsService(engine)
    sr = svc.sample_rate
    OUT.mkdir(parents=True, exist_ok=True)
    import soundfile as sf
    from faster_whisper import WhisperModel
    whisper = WhisperModel("large-v3", device="cpu", compute_type="int8",
                           download_root=str(ROOT / "models" / "hf"), local_files_only=True)

    rows = []
    for seed in range(args.seeds):
        for ti, term in enumerate(terms):
            for fname, frame in FRAMES.items():
                text = " ".join(s.text for s in svc.plan(frame.format(term)))
                np.random.seed(1000 * seed + ti)
                audio = np.concatenate(list(engine.stream(text, "vi")))
                sf.write(str(OUT / f"s{seed}_{ti:02d}_{fname}.wav"), audio, sr, subtype="PCM_16")
                segs, _ = whisper.transcribe(to16k(audio, sr), language="vi", beam_size=5,
                                             condition_on_previous_text=False, vad_filter=False)
                hyp = " ".join(s.text for s in segs).strip()
                row = {"seed": seed, "term": term, "frame": fname, "text": text, "hyp": hyp, "ok": has_term(hyp, term)}
                rows.append(row)
                print(f"s{seed} {term:12s} {fname:8s} {'ok ' if row['ok'] else 'SAI'} {text} → {hyp}", flush=True)
        (OUT / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\nWhisper nghe ra đúng thuật ngữ, theo vị trí trong câu:")
    for fname in FRAMES:
        ok = [r["ok"] for r in rows if r["frame"] == fname]
        print(f"  {fname:8s} {np.mean(ok) * 100:5.0f}% ({sum(ok)}/{len(ok)})   {FRAMES[fname]}")
    print("\nTheo thuật ngữ (số lần đúng / số seed):")
    print(f"  {'':12s}" + "".join(f"{f:>9s}" for f in FRAMES))
    for term in terms:
        cells = [sum(r["ok"] for r in rows if r["term"] == term and r["frame"] == f) for f in FRAMES]
        print(f"  {term:12s}" + "".join(f"{c:>7d}/{args.seeds}" for c in cells))


if __name__ == "__main__":
    main()
