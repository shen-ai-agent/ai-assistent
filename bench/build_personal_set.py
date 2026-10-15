"""Dựng bộ test cá nhân từ các lượt nói thật đã sửa tay (T-007).

Mỗi lượt có bản sửa và còn audio thành một câu test: text = bản sửa, terms = các thuật ngữ glossary
có trong câu, group = "real", split = dev/holdout theo turn_id (giống vòng học trên server).
Audio không chép, manifest trỏ thẳng tới _local/logs/audio/.

  python bench/build_personal_set.py
  python bench/stt_bench.py --testset _local/personal --config v3_vi_topic_oracle

Bộ test lớn dần theo thời gian dùng; chạy lại script để cập nhật.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.learning import load_turns  # noqa: E402
from server.stt.glossary import load_glossary  # noqa: E402
from server.stt.textnorm import contains_term, normalize  # noqa: E402


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs", type=Path, default=ROOT / "_local" / "logs")
    ap.add_argument("--out", type=Path, default=ROOT / "_local" / "personal")
    args = ap.parse_args()

    glossary = load_glossary()
    items = []
    for t in load_turns(args.logs):
        if t.fixed is None or t.audio is None or not t.audio.exists():
            continue
        ref = normalize(t.fixed)
        terms = list(dict.fromkeys(g.text for g in glossary if contains_term(ref, g.text)))
        # ".NET" nằm trong "ASP.NET" thì chỉ tính thuật ngữ dài
        terms = [a for a in terms if not any(a != b and contains_term(normalize(b), a) for b in terms)]
        items.append({
            "id": t.turn_id, "file": Path(os.path.relpath(t.audio, args.out)).as_posix(),
            "text": t.fixed, "terms": terms, "group": "real", "split": t.split,
            "duration": round(sf.info(t.audio).duration, 2), "topic_used": t.topic, "heard": t.text,
        })

    args.out.mkdir(parents=True, exist_ok=True)
    manifest = args.out / "transcripts.jsonl"
    manifest.write_text("".join(json.dumps(it, ensure_ascii=False) + "\n" for it in items), encoding="utf-8")
    dev = sum(it["split"] == "dev" for it in items)
    print(f"{len(items)} câu ({dev} dev, {len(items) - dev} holdout), "
          f"{sum(it['duration'] for it in items) / 60:.1f} phút, {sum(len(it['terms']) for it in items)} thuật ngữ")
    print(f"→ {manifest}")


if __name__ == "__main__":
    main()
