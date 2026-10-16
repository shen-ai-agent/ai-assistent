"""Đo DoD Phase 4 qua WebSocket của server: thả phím → nghe câu đầu, và Claude nhớ ngữ cảnh.

Server phải đang chạy, [bridge] kind = "claude_cli", auto_ask = true, [tts] bật. Mỗi câu hỏi:
  1. nhờ chính server đọc câu hỏi (speak) → audio 48kHz → hạ về 16kHz, coi như người nói
  2. gửi như lượt push-to-talk (start, frame 100ms, stop); server nhận dạng rồi tự hỏi Claude
  3. đo từ lúc gửi stop (thả phím): transcript, text đầu của Claude, mẩu audio đầu của câu trả lời
Câu 3 hỏi lại "dịch vụ đầu tiên" để kiểm tra --resume giữ ngữ cảnh (câu trả lời phải nhắc EC2).
Mỗi lượt tốn hạn mức Claude như hỏi thật (~$0.02–0.05 theo số liệu CLI).

  python bench/claude_ws_client.py            # 5 câu mặc định, session mới
  python bench/claude_ws_client.py --text     # hỏi bằng text (bỏ STT), so sánh độ trễ
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[1]
FRAME = 1600
OUT = ROOT / "_local" / "bench" / "claude"

QUESTIONS = [
    ("EC2 là gì, giải thích ngắn gọn giúp mình.", None),
    ("Còn S3 thì khác nó chỗ nào?", None),
    ("Dịch vụ đầu tiên mình hỏi thì tính tiền theo cách nào?", "EC2"),  # phải nhớ câu 1
    ("Trong C sharp, async await hoạt động thế nào?", None),
    ("What is the difference between IELTS and TOEIC?", None),
]


def recv_json(ws, until: str) -> dict:
    while True:
        raw = ws.recv()
        if isinstance(raw, bytes):
            continue
        msg = json.loads(raw)
        if msg["type"] == until:
            return msg
        if msg["type"] == "error":
            raise RuntimeError(msg)


def synth_question(ws, text: str) -> np.ndarray:
    """Server đọc câu hỏi; ghép audio rồi hạ 48k → 16k (trung bình 3 mẫu, đủ cho Whisper)."""
    ws.send(json.dumps({"type": "speak", "text": text}))
    speak_id, chunks = None, []
    while True:
        raw = ws.recv()
        if isinstance(raw, bytes):
            if int.from_bytes(raw[:4], "little") == speak_id:
                chunks.append(np.frombuffer(raw[4:], "<i2"))
            continue
        msg = json.loads(raw)
        if msg["type"] == "speak_start":
            speak_id = msg["speak_id"]
        elif msg["type"] == "speak_end" and msg["speak_id"] == speak_id:
            break
    pcm = np.concatenate(chunks).astype(np.float32)
    pcm = pcm[: len(pcm) // 3 * 3].reshape(-1, 3).mean(axis=1)
    silence = np.zeros(4800, dtype=np.float32)  # 0.3s trước/sau như người bấm phím
    return np.concatenate([silence, pcm, silence]).astype("<i2")


def run_turn(ws, question: str, audio: np.ndarray | None, topic: str | None) -> dict:
    if audio is not None:
        ws.send(json.dumps({"type": "start", "topic": topic}))
        for i in range(0, len(audio), FRAME):
            ws.send(audio[i:i + FRAME].tobytes())
        ws.send(json.dumps({"type": "stop"}))
    else:
        ws.send(json.dumps({"type": "ask", "text": question}))
    t0 = time.perf_counter()
    ms = lambda: round((time.perf_counter() - t0) * 1000)  # noqa: E731
    row = {"question": question, "transcript": None, "transcript_ms": None, "first_text_ms": None,
           "first_audio_ms": None, "answer": "", "speak_id": None}
    answer_done = speak_done = False
    while not (answer_done and (speak_done or row["speak_id"] is None)):
        raw = ws.recv()
        if isinstance(raw, bytes):
            if int.from_bytes(raw[:4], "little") == row["speak_id"] and row["first_audio_ms"] is None:
                row["first_audio_ms"] = ms()
            continue
        msg = json.loads(raw)
        kind = msg["type"]
        if kind == "transcript":
            row["transcript"], row["transcript_ms"] = msg["text"], ms()
        elif kind == "answer_start":
            row["speak_id"] = msg["speak_id"]
        elif kind == "answer_delta":
            if row["first_text_ms"] is None:
                row["first_text_ms"] = ms()
            row["answer"] += msg["text"]
        elif kind == "answer_end":
            answer_done = True
            row.update(total_ms=ms(), error=msg["error"], cost_usd=msg["cost_usd"], session_id=msg["session_id"])
        elif kind == "speak_end" and msg["speak_id"] == row["speak_id"]:
            speak_done = True
            row["audio_s"] = round(msg["audio_ms"] / 1000, 1)
        elif kind == "error":
            print(f"  lỗi: {msg}")
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8765/ws")
    ap.add_argument("--text", action="store_true", help="hỏi bằng text, bỏ qua STT")
    ap.add_argument("--keep-session", action="store_true", help="không mở session mới trước khi đo")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    rows = []
    with connect(args.url, max_size=None, open_timeout=30) as ws:
        hello = json.loads(ws.recv())
        if not hello.get("bridge") or not hello.get("tts"):
            sys.exit("Server cần bật [bridge] và [tts]")
        if not hello["bridge"]["auto_ask"] and not args.text:
            sys.exit("Server cần [bridge] auto_ask = true (hoặc chạy --text)")
        if not args.keep_session:
            ws.send(json.dumps({"type": "new_session"}))
            recv_json(ws, "bridge_session")
        for i, (q, must) in enumerate(QUESTIONS, 1):
            audio = None if args.text else synth_question(ws, q)
            row = run_turn(ws, q, audio, None)
            row["context_ok"] = None if must is None else must.lower() in row["answer"].lower()
            rows.append(row)
            print(f"{i}. {q}")
            if row["transcript"] is not None:
                print(f"   STT: {row['transcript']}  ({row['transcript_ms']}ms)")
            print(f"   text đầu {row['first_text_ms']}ms · nghe câu đầu {row['first_audio_ms']}ms · "
                  f"trả lời xong {row.get('total_ms')}ms · ${row.get('cost_usd') or 0:.3f}"
                  + (f" · ngữ cảnh {'OK' if row['context_ok'] else 'SAI'}" if must else "")
                  + (f" · LỖI {row['error']}" if row.get("error") else ""))
            print(f"   → {row['answer'][:160].replace(chr(10), ' ')}…")

    firsts = sorted(r["first_audio_ms"] for r in rows if r["first_audio_ms"] is not None)
    if firsts:
        print(f"\nthả phím → nghe câu đầu: p50 {firsts[len(firsts) // 2]}ms, max {firsts[-1]}ms (DoD ≤ 8000ms)")
    print(f"tổng chi phí ${sum(r.get('cost_usd') or 0 for r in rows):.3f}")
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{time.strftime('%Y%m%d-%H%M%S')}{'-text' if args.text else ''}.json"
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Lưu {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
