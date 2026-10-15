"""Đẩy audio testset qua WebSocket của server như trình duyệt (frame 100ms), chấm như stt_bench.

Server phải đang chạy (python -m server). Dùng để:
  - kiểm tra pipeline server cho kết quả giống benchmark:   python bench/ws_client.py --limit 20
  - chạy bền, xem VRAM có tăng dần không:                    python bench/ws_client.py --turns 50
  - chấm audio thật đã đi qua trình duyệt (_local/logs/audio) thì dùng --from-logs

  --topic oracle   gửi chủ đề lấy từ đáp án (người dùng tự chọn)  [mặc định]
  --topic auto     gửi topic null, server tự đoán (two_pass)
  --realtime       gửi frame đúng nhịp 100ms như mic thật (mặc định gửi dồn)
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
import time
from pathlib import Path

import numpy as np
from websockets.sync.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stt_bench import VramMonitor, detect_topic, load_glossary, load_testset, make_row, summarize  # noqa: E402

FRAME = 1600


def run_turn(ws, audio: np.ndarray, topic: str | None, realtime: bool) -> tuple[dict, float]:
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    ws.send(json.dumps({"type": "start", "topic": topic}))
    for i in range(0, len(pcm), FRAME):
        ws.send(pcm[i:i + FRAME].tobytes())
        if realtime:
            time.sleep(FRAME / 16000)
    ws.send(json.dumps({"type": "stop"}))
    released = time.perf_counter()
    while True:
        msg = json.loads(ws.recv())
        if msg["type"] == "transcript":
            return msg, time.perf_counter() - released
        if msg["type"] == "error":
            raise RuntimeError(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8765/ws")
    ap.add_argument("--limit", type=int, help="chỉ chạy N câu đầu")
    ap.add_argument("--turns", type=int, help="chạy đúng N lượt (lặp lại testset nếu cần)")
    ap.add_argument("--topic", choices=("oracle", "auto"), default="oracle")
    ap.add_argument("--realtime", action="store_true")
    args = ap.parse_args()

    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    items = load_testset()[: args.limit]
    if args.turns:
        items = list(itertools.islice(itertools.cycle(items), args.turns))
    glossary = load_glossary()
    vram = VramMonitor()
    vram_samples = []

    rows = []
    with connect(args.url, max_size=None) as ws:
        hello = json.loads(ws.recv())
        print(f"Server: {hello['model']}, chủ đề {hello['topics']}")
        for i, it in enumerate(items, 1):
            topic = detect_topic(it["text"], glossary) if args.topic == "oracle" else None
            msg, latency = run_turn(ws, it["audio"], topic, args.realtime)
            rows.append({**make_row(it, msg["text"], "", 0.0, latency, None), "stt_ms": msg["stt_ms"]})
            vram_samples.append(vram.used())
            print(f"\r  {i}/{len(items)}  {it['id']:<10} {latency * 1000:5.0f}ms  "
                  f"VRAM {vram_samples[-1] / 2**30:.2f}GB", end="", flush=True)
    print()
    for r in rows[: len(set(r["id"] for r in rows))]:
        if r["missed"]:
            print(f"  trượt {r['id']}: {r['missed']}  ← {r['hyp']}")

    scores = summarize(rows[: len(set(r["id"] for r in rows))])  # chấm mỗi câu một lần
    lat = sorted(r["latency"] for r in rows)
    print(f"WER {scores['all']['wer']:.1%}  term recall {scores['all']['term_recall'] or 0:.1%}"
          f"  ({scores['all']['n']} câu)")
    print(f"Thả phím → text: p50 {lat[len(lat) // 2] * 1000:.0f}ms  p90 {lat[int(len(lat) * 0.9)] * 1000:.0f}ms"
          f"  max {lat[-1] * 1000:.0f}ms")
    q = max(1, len(vram_samples) // 4)
    first, last = np.mean(vram_samples[:q]), np.mean(vram_samples[-q:])
    print(f"VRAM: 1/4 đầu {first / 2**30:.2f}GB → 1/4 cuối {last / 2**30:.2f}GB "
          f"(chênh {(last - first) / 2**20:+.0f}MB)")


if __name__ == "__main__":
    main()
