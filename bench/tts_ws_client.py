"""Đo DoD Phase 3 qua WebSocket của server, như trình duyệt: gửi speak, nhận audio, giả lập lịch phát.

Server phải đang chạy (python -m server) và bật [tts]. Đo trên bench/tts_samples.txt:
  - tiếng đầu: gửi speak → mẩu audio đầu tới (DoD ≤ 1.5s)
  - hụt: giả lập phát đúng nhịp như web/app.js; lần hết audio lâu nhất khi chờ mẩu sau (DoD ≤ 0.5s)
  - dừng: đọc đoạn dài, sau --stop-after giây gửi speak_stop, đo tới speak_end và frame còn tới sau đó
  - VRAM: đỉnh của cả GPU trong lúc đo (STT + TTS cùng nằm trong server; DoD ≤ 11GB)

  python bench/tts_ws_client.py              # cả 35 đoạn + 5 lần thử dừng
  python bench/tts_ws_client.py --only t01 d03
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from pathlib import Path

from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bench.stt_bench import VramMonitor  # noqa: E402
from bench.tts_bench import load_samples  # noqa: E402

SENTENCE_GAP_S = 0.15  # giống web/app.js
PLAY_LEAD_S = 0.05
LEADS = (0.05, 0.15, 0.3, 0.5)  # mô phỏng lại lịch phát với các mức đệm mẩu đầu khác nhau
STUTTER_S = 0.005               # hụt giữa câu dài hơn chừng này là nghe thấy giật


def simulate(arrivals: list[tuple[float, float, bool]], lead: float) -> tuple[int, float]:
    """Phát lại các mẩu theo giờ tới như web/app.js. arrivals: (giờ tới, độ dài, đầu câu mới).

    Trả (số lần giật giữa câu, tổng thời gian giật). Giữa hai câu đã có khoảng lặng cố ý nên không tính."""
    next_time, n, total = 0.0, 0, 0.0
    for i, (now, dur, new_sentence) in enumerate(arrivals):
        if i == 0:
            start = now + lead
        else:
            due = next_time + (SENTENCE_GAP_S if new_sentence else 0.0)
            if not new_sentence and now - due > STUTTER_S:
                n += 1
                total += now - due
            start = max(due, now)
        next_time = start + dur
    return n, total


def stt_load(url: str, stop: threading.Event, counter: list[int]):
    """Kết nối thứ hai: đẩy liên tục audio testset qua STT, như lúc rảnh tay mic vẫn nghe khi đang đọc."""
    import soundfile as sf

    files = sorted((ROOT / "data" / "testset").glob("*.wav"))[:20]
    with connect(url, max_size=None) as ws:
        ws.recv()
        while not stop.is_set():
            for f in files:
                if stop.is_set():
                    break
                audio, _ = sf.read(f, dtype="int16")
                ws.send(json.dumps({"type": "start", "topic": None}))
                ws.send(audio.tobytes())
                ws.send(json.dumps({"type": "stop"}))
                while True:
                    raw = ws.recv()
                    if isinstance(raw, str) and json.loads(raw)["type"] in ("transcript", "error"):
                        break
                counter[0] += 1


def speak(ws, text: str, sample_rate: int, stop_after: float | None = None) -> dict:
    """Một lượt đọc. Trả số đo; stop_after thì gửi speak_stop sau ngần ấy giây kể từ mẩu đầu."""
    t0 = time.perf_counter()
    ws.send(json.dumps({"type": "speak", "text": text}))
    speak_id = None
    n_sent = 0
    first = None
    next_time = 0.0       # giờ phát (theo t0) mẩu kế tiếp bắt đầu
    pending_gap = 0.0
    max_gap = 0.0
    stop_sent = None
    late_frames = 0
    arrivals: list[tuple[float, float, bool]] = []
    new_sentence = False
    while True:
        timeout = None
        if stop_after is not None and stop_sent is None and first is not None:
            timeout = max(0.0, first + stop_after - (time.perf_counter() - t0))
        try:
            raw = ws.recv(timeout=timeout)
        except TimeoutError:
            ws.send(json.dumps({"type": "speak_stop"}))
            stop_sent = time.perf_counter() - t0
            continue
        now = time.perf_counter() - t0
        if isinstance(raw, bytes):
            if int.from_bytes(raw[:4], "little") != speak_id:
                continue
            if stop_sent is not None:
                late_frames += 1
                continue
            dur = (len(raw) - 4) / 2 / sample_rate
            arrivals.append((now, dur, new_sentence))
            new_sentence = False
            if first is None:
                first = now
                start = now + PLAY_LEAD_S
            else:
                due = next_time + pending_gap
                if due < now:
                    max_gap = max(max_gap, now - next_time)
                start = max(due, now)
            pending_gap = 0.0
            next_time = start + dur
            continue
        msg = json.loads(raw)
        if msg["type"] == "speak_start":
            speak_id = msg["speak_id"]
        elif msg["type"] == "speak_more" and msg["speak_id"] == speak_id:
            n_sent += len(msg["sentences"])
        elif msg["type"] == "speak_sentence" and msg["speak_id"] == speak_id and msg["index"] > 0:
            pending_gap = SENTENCE_GAP_S
            new_sentence = True
        elif msg["type"] == "speak_end" and msg["speak_id"] == speak_id:
            return {"first_ms": round(first * 1000) if first is not None else None,
                    "stutter": {lead: simulate(arrivals, lead) for lead in LEADS},
                    "max_gap_ms": round(max_gap * 1000), "sentences": n_sent,
                    "audio_ms": msg["audio_ms"], "synth_ms": msg["synth_ms"], "ttfa_ms": msg["ttfa_ms"],
                    "cancelled": msg["cancelled"],
                    "stop_to_end_ms": round((now - stop_sent) * 1000) if stop_sent is not None else None,
                    "late_frames": late_frames}
        elif msg["type"] == "error":
            raise RuntimeError(msg)


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(q * (len(xs) - 1)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8765/ws")
    ap.add_argument("--only", nargs="*", help="chỉ chạy các id này")
    ap.add_argument("--stops", type=int, default=5, help="số lần thử dừng giữa chừng")
    ap.add_argument("--stop-after", type=float, default=1.0)
    ap.add_argument("--stt-load", action="store_true", help="cho STT chạy song song (giống rảnh tay khi đang đọc)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    samples = [s for s in load_samples() if not args.only or s["id"] in args.only]
    vram = VramMonitor()
    vram.start()
    load_stop, load_turns = threading.Event(), [0]
    if args.stt_load:
        threading.Thread(target=stt_load, args=(args.url, load_stop, load_turns), daemon=True).start()
    with connect(args.url, max_size=None) as ws:
        hello = json.loads(ws.recv())
        if not hello.get("tts"):
            sys.exit("Server đang tắt TTS ([tts] enabled trong server/config.toml).")
        sr = hello["tts"]["sample_rate"]

        rows = []
        for s in samples:
            r = speak(ws, s["text"], sr) | {"id": s["id"], "group": s["group"]}
            rows.append(r)
            n, _ = r["stutter"][PLAY_LEAD_S]
            print(f"{r['id']:4} {r['group']:5} câu={r['sentences']:2}  tiếng đầu {r['first_ms']:5}ms  "
                  f"hụt {r['max_gap_ms']:4}ms  giật {n:2}  audio {r['audio_ms'] / 1000:5.1f}s  "
                  f"RTF {r['synth_ms'] / r['audio_ms']:.2f}")

        long_text = " ".join(s["text"] for s in samples if s["group"] == "clean")
        stops = [speak(ws, long_text, sr, stop_after=args.stop_after) for _ in range(args.stops)]
    load_stop.set()
    vram.stop()

    first = [r["first_ms"] for r in rows]
    gaps = [r["max_gap_ms"] for r in rows]
    print(f"\n{len(rows)} đoạn")
    print(f"  tiếng đầu   p50 {statistics.median(first):.0f}ms  p95 {pct(first, .95):.0f}ms  max {max(first)}ms   (DoD ≤ 1500)")
    print(f"  hụt         p50 {statistics.median(gaps):.0f}ms  p95 {pct(gaps, .95):.0f}ms  max {max(gaps)}ms   (DoD ≤ 500)")
    print(f"  RTF         {sum(r['synth_ms'] for r in rows) / sum(r['audio_ms'] for r in rows):.2f}")
    audio_min = sum(r["audio_ms"] for r in rows) / 60000
    for lead in LEADS:
        n = sum(r["stutter"][lead][0] for r in rows)
        total = sum(r["stutter"][lead][1] for r in rows)
        hit = sum(r["stutter"][lead][0] > 0 for r in rows)
        print(f"  giật giữa câu, đệm mẩu đầu {lead * 1000:3.0f}ms: {n:3} lần ({n / audio_min:.1f}/phút audio), "
              f"{hit}/{len(rows)} đoạn bị, tổng {total * 1000:.0f}ms" + ("   ← web đang dùng" if lead == PLAY_LEAD_S else ""))
    if args.stt_load:
        print(f"  STT chạy song song: {load_turns[0]} lượt")
    if stops:
        ends = [r["stop_to_end_ms"] for r in stops]
        print(f"  dừng → speak_end  p50 {statistics.median(ends):.0f}ms  max {max(ends)}ms; "
              f"frame tới sau speak_stop: max {max(r['late_frames'] for r in stops)} (web bỏ qua, tắt tiếng ngay)")
    print(f"  VRAM đỉnh cả GPU  {vram.peak / 2**20:.0f}MB   (DoD ≤ 11264)")


if __name__ == "__main__":
    main()
