"""Đo nhịp giảng từ file audio (bài giảng thật, hoặc wav TTS từ bench/tts_render.py) để chỉnh normalize.PAUSES.

    .venv/Scripts/python bench/lecture_prosody.py _local/lecture/audio/*.m4a
    .venv/Scripts/python bench/lecture_prosody.py _local/tts_render/*.wav --tag tts
    .venv/Scripts/python bench/lecture_prosody.py --cached            # đo lại từ kết quả Whisper đã lưu, không cần GPU

Chạy Whisper large-v3 có mốc thời gian từng từ, lưu vào _local/lecture/words/<tên>.json, rồi in khoảng nghỉ theo
dấu câu, tốc độ nói, chỗ ngừng không có dấu câu (thường là trước ý muốn nhấn) và PAUSES đề xuất.
Tắt server trước (GPU). Audio bài giảng chỉ để đo, không dùng để tạo giọng; để trong _local/ (không commit).
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.tts.normalize import PAUSES  # noqa: E402
from server.tts.prosody import KINDS, Word, measure, merge_gaps, suggest_pauses  # noqa: E402

OUT = ROOT / "_local" / "lecture"
WORDS = OUT / "words"
NAMES = {"none": "không dấu", "comma": "phẩy", "colon": "hai chấm", "sentence": "chấm", "question": "hỏi"}


def transcribe(paths: list[Path], model: str, device: str) -> None:
    from server.stt.engine import WhisperEngine  # nạp cublas từ torch/lib
    eng = WhisperEngine(model, "int8_float16" if device == "cuda" else "int8", device)
    WORDS.mkdir(parents=True, exist_ok=True)
    for p in paths:
        print(f"Whisper: {p.name} ...", flush=True)
        # condition_on_previous_text: giữ cách chấm câu nhất quán suốt bài dài; vad_filter: bỏ nhạc/im lặng dài
        segments, _ = eng.model.transcribe(str(p), language="vi", beam_size=5, word_timestamps=True,
                                           vad_filter=True, condition_on_previous_text=True)
        words = [{"w": w.word, "s": round(w.start, 3), "e": round(w.end, 3)} for s in segments for w in s.words]
        (WORDS / f"{p.stem}.json").write_text(json.dumps({"source": str(p), "words": words}, ensure_ascii=False),
                                              encoding="utf-8")


def load(stem: str) -> list[Word]:
    data = json.loads((WORDS / f"{stem}.json").read_text(encoding="utf-8"))
    return [Word(w["w"], w["s"], w["e"]) for w in data["words"]]


def show(name: str, st: dict) -> None:
    g = st["gaps"]
    cells = "  ".join(f"{NAMES[k]} {g[k]['median']}ms(n={g[k]['n']})" for k in KINDS if g[k].get("n"))
    print(f"{name:28s} {st['minutes']:5.1f}ph  nói {st['articulation_rate']} âm tiết/s"
          f" (cả ngừng {st['speech_rate']})  kéo dài cuối câu x{st['boundary_lengthening']}"
          f"  ngừng dài {st['long_pauses_per_min']}/ph\n    {cells}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", help="file audio (wav/mp3/m4a...), nhận cả glob")
    ap.add_argument("--cached", action="store_true", help="dùng mọi file đã có trong _local/lecture/words")
    ap.add_argument("--tag", default="", help="chỉ lấy file words có tên bắt đầu bằng tag (khi --cached)")
    ap.add_argument("--model", default="large-v3")
    ap.add_argument("--cpu", action="store_true")
    ap.add_argument("--show-pauses", type=int, default=12, help="in N chỗ ngừng dài nhất không có dấu câu")
    args = ap.parse_args()

    paths = [Path(f) for pat in args.files for f in (glob.glob(pat) or [pat])]
    todo = [p for p in paths if not (WORDS / f"{p.stem}.json").exists()]
    if todo:
        transcribe(todo, args.model, "cpu" if args.cpu else "cuda")
    stems = [p.stem for p in paths] if paths else \
        sorted(f.stem for f in WORDS.glob(f"{args.tag}*.json")) if args.cached else []
    if not stems:
        ap.error("chưa có file nào để đo")

    all_words = []
    for stem in stems:
        words = load(stem)
        all_words.append(words)
        st = measure(words)
        show(stem, st)
        for x in st["unpunct_pauses"][:args.show_pauses]:
            print(f"      {x['ms']:5d}ms  {x['at']}")

    gaps = merge_gaps(all_words)
    print("\nGộp tất cả (ms):")
    for k in KINDS:
        if gaps[k].get("n"):
            g = gaps[k]
            print(f"  {NAMES[k]:10s} n={g['n']:5d}  p25 {g['p25']:4d}  trung vị {g['median']:4d}  p75 {g['p75']:4d}"
                  f"  p90 {g['p90']:4d}")
    print(f"\nPAUSES hiện tại: {PAUSES}")
    print(f"PAUSES đề xuất (đã trừ ~180ms lặng sẵn của ZeroTTS): {suggest_pauses(gaps)}")


if __name__ == "__main__":
    main()
