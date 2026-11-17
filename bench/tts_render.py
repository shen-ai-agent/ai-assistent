"""Xuất câu trả lời thật thành wav để nghe so sánh cách ngắt nghỉ (A/B), và để đo bằng bench/lecture_prosody.py.

    .venv/Scripts/python bench/tts_render.py --session _local/sessions/2026-11-17.jsonl --last 3
    .venv/Scripts/python bench/tts_render.py --text "Câu một. Câu hai?" --name thu

Mỗi câu chỉ tổng hợp một lần rồi ghép thành hai bản, nên hai bản chỉ khác nhau ở khoảng nghỉ:
  <tên>_cu.wav    như trước: nghỉ cố định 150ms giữa mọi câu (SENTENCE_GAP_S cũ của web)
  <tên>_moi.wav   nghỉ theo dấu câu / chuyển đoạn (normalize.pause_ms)
Mặc định chạy CPU để không tranh GPU với server (RTF ~1); --gpu khi server đã tắt.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.tts.synth import TtsService, ZeroTtsEngine  # noqa: E402

OUT = ROOT / "_local" / "tts_render"
OLD_GAP_MS = 150


def save(path: Path, audio: np.ndarray, sr: int) -> None:
    import soundfile as sf
    sf.write(str(path), audio, sr, subtype="PCM_16")


def render(svc: TtsService, text: str) -> tuple[np.ndarray, np.ndarray, list]:
    sr = svc.sample_rate
    old, new, segs = [], [], svc.plan(text)
    for i, seg in enumerate(segs):
        audio = np.concatenate(list(svc.stream(seg)))
        if i:
            old.append(np.zeros(sr * OLD_GAP_MS // 1000, np.float32))
            new.append(np.zeros(sr * segs[i - 1].pause_ms // 1000, np.float32))
        old.append(audio)
        new.append(audio)
    return np.concatenate(old), np.concatenate(new), segs


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", type=Path, help="file jsonl trong _local/sessions")
    ap.add_argument("--last", type=int, default=3, help="lấy N câu trả lời dài nhất trong số 3N câu cuối")
    ap.add_argument("--text")
    ap.add_argument("--name", default="text")
    ap.add_argument("--voice", default="maichi")
    ap.add_argument("--gpu", action="store_true")
    args = ap.parse_args()

    items = []
    if args.text:
        items.append((args.name, args.text))
    if args.session:
        rows = [json.loads(x) for x in args.session.read_text(encoding="utf-8").splitlines() if x.strip()]
        rows = [r for r in rows if r.get("answer") and not r.get("error")][-3 * args.last:]
        rows = sorted(rows, key=lambda r: -len(r["answer"]))[:args.last]
        items += [(f"{args.session.stem}_{r['ts'][11:19].replace(':', '')}", r["answer"]) for r in rows]
    if not items:
        ap.error("cần --text hoặc --session")

    engine = ZeroTtsEngine(args.voice, "cuda" if args.gpu else "cpu", 4)
    svc = TtsService(engine)
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in items:
        old, new, segs = render(svc, text)
        save(OUT / f"{name}_cu.wav", old, svc.sample_rate)
        save(OUT / f"{name}_moi.wav", new, svc.sample_rate)
        (OUT / f"{name}.txt").write_text(
            "\n".join(f"[nghỉ sau {s.pause_ms}ms] {s.source}" for s in segs), encoding="utf-8")
        print(f"{name}: {len(segs)} câu, cũ {len(old) / svc.sample_rate:.1f}s, mới {len(new) / svc.sample_rate:.1f}s")


if __name__ == "__main__":
    main()
