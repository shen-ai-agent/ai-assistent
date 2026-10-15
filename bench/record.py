"""Ghi âm bộ test STT từ data/sentences.tsv.

Mỗi câu lưu thành data/testset/<id>.wav (16kHz, mono, 16-bit) và một dòng trong
data/testset/transcripts.jsonl. Chạy lại thì bỏ qua các câu đã ghi.

    python bench/record.py                    # ghi tiếp các câu còn thiếu
    python bench/record.py --list-devices     # xem danh sách micro
    python bench/record.py --device 3         # chọn micro theo số thứ tự
    python bench/record.py --group mix        # chỉ ghi một nhóm
    python bench/record.py --redo mix_07      # ghi lại một câu
    python bench/record.py --test-mic         # kiểm tra mức thu trước khi ghi
    python bench/record.py --restart          # cất bộ đã ghi vào _local/ rồi ghi lại từ đầu
"""

from __future__ import annotations

import argparse
import csv
import json
import msvcrt
import os
import queue
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
import soxr

ROOT = Path(__file__).resolve().parent.parent
SENTENCES = ROOT / "data" / "sentences.tsv"
OUT_DIR = ROOT / "data" / "testset"
MANIFEST = OUT_DIR / "transcripts.jsonl"

SAMPLE_RATE = 16000
MIN_SECONDS = 0.5
QUIET_DB = -30.0      # đỉnh thấp hơn mức này: có thể nói quá nhỏ / sai micro
CLIP_LEVEL = 0.99     # mẫu có biên độ từ mức này trở lên coi như bị cắt
CLIP_MAX_RATIO = 1e-3 # quá 0.1% số mẫu bị cắt: vỡ tiếng, không nên lưu
GOOD_PEAK_DB = (-12.0, -3.0)  # khoảng đỉnh lý tưởng khi nói bình thường


def load_sentences() -> list[dict]:
    with SENTENCES.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    for r in rows:
        r["terms"] = [t.strip() for t in (r.get("terms") or "").split(";") if t.strip()]
    return rows


def load_manifest() -> dict[str, dict]:
    if not MANIFEST.exists():
        return {}
    with MANIFEST.open(encoding="utf-8") as f:
        items = [json.loads(line) for line in f if line.strip()]
    return {it["id"]: it for it in items}


def save_manifest(manifest: dict[str, dict], order: list[str]) -> None:
    tmp = MANIFEST.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for sid in order:
            if sid in manifest:
                f.write(json.dumps(manifest[sid], ensure_ascii=False) + "\n")
    os.replace(tmp, MANIFEST)


def peak_db(audio: np.ndarray) -> float:
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    return 20 * np.log10(peak) if peak > 0 else -120.0


def clip_ratio(audio: np.ndarray) -> float:
    return float(np.mean(np.abs(audio) >= CLIP_LEVEL)) if audio.size else 0.0


def check_audio(audio: np.ndarray) -> tuple[bool, str]:
    """Trả về (đạt, mô tả). Không đạt khi vỡ tiếng, quá nhỏ hoặc quá ngắn."""
    dur = len(audio) / SAMPLE_RATE
    db, cr = peak_db(audio), clip_ratio(audio)
    info = f"{dur:.1f}s · đỉnh {db:.1f} dBFS · bị cắt {cr * 100:.2f}%"
    if dur < MIN_SECONDS:
        return False, info + "  ⚠ quá ngắn"
    if cr > CLIP_MAX_RATIO:
        return False, info + "  ⛔ VỠ TIẾNG — giảm âm lượng micro trong Windows rồi ghi lại"
    if db < QUIET_DB:
        return False, info + "  ⚠ quá nhỏ — kiểm tra micro"
    return True, info


def restart_testset() -> None:
    if not OUT_DIR.exists() or not any(OUT_DIR.iterdir()):
        return
    backup = ROOT / "_local" / f"testset_backup_{datetime.now():%Y%m%d_%H%M%S}"
    backup.parent.mkdir(exist_ok=True)
    shutil.move(str(OUT_DIR), backup)
    OUT_DIR.mkdir(parents=True)
    print(f"Đã cất bộ cũ vào {backup.relative_to(ROOT)}")


def test_mic(device: int | None, rate: int) -> None:
    """Ghi thử nhiều lần để chỉnh mức thu, không lưu file."""
    lo, hi = GOOD_PEAK_DB
    print("\nKiểm tra micro: nói một câu dài, to như khi hỏi thật (ví dụ câu nhiều thuật ngữ).")
    print(f"Mục tiêu: đỉnh trong khoảng {lo:.0f} … {hi:.0f} dBFS và bị cắt 0.00%.")
    while True:
        print("\n  Enter: ghi thử   q: thoát")
        key = ""
        while key not in ("\r", "q"):
            key = getkey()
        if key == "q":
            return
        audio = record_once(device, rate)
        ok, info = check_audio(audio)
        db = peak_db(audio)
        if ok and db > hi:
            info += "  · hơi lớn, nên giảm thêm chút"
        elif ok and db < lo:
            info += "  · hơi nhỏ, có thể tăng thêm chút"
        elif ok:
            info += "  ✅ mức thu tốt"
        print("  " + info)


def getkey() -> str:
    """Đọc một phím, không cần Enter. Trả về '\\r' cho Enter."""
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):  # phím đặc biệt (mũi tên...) — bỏ qua
        msvcrt.getwch()
        return ""
    return ch.lower()


def level_bar(db: float, width: int = 30) -> str:
    filled = int(np.clip((db + 60) / 60, 0, 1) * width)
    return "█" * filled + "·" * (width - filled)


def capture_rate(device: int | None) -> int:
    """Thu thẳng 16kHz nếu micro hỗ trợ, không thì thu ở tần số gốc rồi resample."""
    try:
        sd.check_input_settings(device=device, samplerate=SAMPLE_RATE, channels=1, dtype="float32")
        return SAMPLE_RATE
    except sd.PortAudioError:
        return int(sd.query_devices(device, "input")["default_samplerate"])


def record_once(device: int | None, rate: int) -> np.ndarray:
    """Ghi từ lúc gọi tới khi bấm Enter, trả về audio 16kHz. Hiện mức âm lượng trong lúc ghi."""
    q: queue.Queue[np.ndarray] = queue.Queue()

    def callback(indata, frames, t, status):
        q.put(indata[:, 0].copy())

    chunks: list[np.ndarray] = []
    with sd.InputStream(samplerate=rate, channels=1, dtype="float32",
                        device=device, callback=callback):
        start = time.time()
        while True:
            while not q.empty():
                chunks.append(q.get())
            recent = chunks[-3:]
            db = peak_db(np.concatenate(recent)) if recent else -120.0
            print(f"\r  🔴 {time.time() - start:5.1f}s  {level_bar(db)}  Enter để dừng ",
                  end="", flush=True)
            if msvcrt.kbhit() and getkey() == "\r":
                break
            time.sleep(0.05)
        time.sleep(0.15)  # lấy nốt phần đuôi câu
    while not q.empty():
        chunks.append(q.get())
    print()
    audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    if rate != SAMPLE_RATE and audio.size:
        audio = soxr.resample(audio, rate, SAMPLE_RATE).astype(np.float32)
    return audio


def show_sentence(s: dict, idx: int, total: int) -> None:
    print("\n" + "─" * 70)
    print(f"[{idx}/{total}] {s['id']}  ·  nhóm: {s['group']}  ·  {s['split']}")
    print(f"\n   {s['text']}\n")
    if s["terms"]:
        print(f"   Thuật ngữ: {', '.join(s['terms'])}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", type=int, help="số thứ tự micro (xem --list-devices)")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--group", help="chỉ ghi nhóm này: vi, mix, dense, en")
    ap.add_argument("--redo", nargs="+", metavar="ID", help="ghi lại các câu này")
    ap.add_argument("--test-mic", action="store_true", help="ghi thử để chỉnh mức thu, không lưu")
    ap.add_argument("--restart", action="store_true", help="cất bộ đã ghi vào _local/ rồi ghi lại từ đầu")
    args = ap.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    try:
        device_name = sd.query_devices(args.device, "input")["name"]
    except (sd.PortAudioError, ValueError):
        print("Không tìm thấy micro mặc định. Xem danh sách bằng --list-devices rồi chọn bằng --device <số>.")
        return
    rate = capture_rate(args.device)
    print(f"Micro: {device_name}  ·  thu ở {rate} Hz")
    if rate < SAMPLE_RATE:
        print(f"⚠ Micro chỉ thu {rate} Hz (thường là tai nghe Bluetooth ở chế độ thoại).")
        print("  Chất lượng STT sẽ thấp hơn rõ rệt. Nên dùng micro có dây / USB nếu được.")

    if args.test_mic:
        test_mic(args.device, rate)
        return
    if args.restart:
        restart_testset()

    sentences = load_sentences()
    order = [s["id"] for s in sentences]
    manifest = load_manifest()

    # Câu đã ghi nhưng text trong TSV đã bị sửa sau đó
    for s in sentences:
        m = manifest.get(s["id"])
        if m and m["text"] != s["text"]:
            print(f"⚠ {s['id']}: text trong sentences.tsv khác lúc ghi — cân nhắc --redo {s['id']}")

    if args.redo:
        todo = [s for s in sentences if s["id"] in set(args.redo)]
    else:
        todo = [s for s in sentences if s["id"] not in manifest]
    if args.group:
        todo = [s for s in todo if s["group"] == args.group]

    print(f"Đã ghi: {len(manifest)}/{len(sentences)}  ·  Lần này: {len(todo)} câu")
    if not todo:
        print("Không còn câu nào cần ghi.")
        return
    print("\nMẹo: đọc câu trong đầu một lần, rồi nói lại tự nhiên như đang hỏi thật.")

    for idx, s in enumerate(todo, 1):
        show_sentence(s, idx, len(todo))
        print("   Enter: bắt đầu ghi   s: bỏ qua   q: thoát")
        key = ""
        while key not in ("\r", "s", "q"):
            key = getkey()
        if key == "q":
            break
        if key == "s":
            continue

        while True:
            audio = record_once(args.device, rate)
            dur = len(audio) / SAMPLE_RATE
            db = peak_db(audio)
            ok, info = check_audio(audio)
            print("  " + info)
            if ok:
                print("  Enter: lưu   r: nghe lại   x: ghi lại   s: bỏ qua")
            else:
                print("  Không lưu được bản này.   r: nghe lại   x: ghi lại   s: bỏ qua")

            action = ""
            while action not in ("\r", "x", "s"):
                action = getkey()
                if action == "r":
                    sd.play(audio, SAMPLE_RATE)
                    sd.wait()
                if action == "\r" and not ok:
                    action = ""
            if action == "x":
                continue
            if action == "\r":
                path = OUT_DIR / f"{s['id']}.wav"
                sf.write(path, audio, SAMPLE_RATE, subtype="PCM_16")
                manifest[s["id"]] = {
                    "id": s["id"],
                    "file": path.name,
                    "text": s["text"],
                    "terms": s["terms"],
                    "group": s["group"],
                    "split": s["split"],
                    "duration": round(dur, 2),
                    "peak_db": round(db, 1),
                    "clip_ratio": round(clip_ratio(audio), 5),
                    "device": device_name,
                    "capture_rate": rate,
                    "recorded_at": datetime.now().isoformat(timespec="seconds"),
                }
                save_manifest(manifest, order)
                print("  ✓ đã lưu")
            break

    print(f"\nTổng đã ghi: {len(manifest)}/{len(sentences)}")


if __name__ == "__main__":
    main()
