"""In các đề xuất học từ log dùng thật, bản dòng lệnh của trang web /learn.html.

Logic nằm ở server/learning.py. Chỉ lượt dev sinh đề xuất; lỗi phải lặp ở ≥ --min lượt.
Muốn duyệt và áp dụng thì dùng trang web: ở đó đề xuất được kiểm tra trên bộ test trước khi ghi.

  python bench/suggest_corrections.py [--min 2]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import learning as L  # noqa: E402
from server.stt.corrections import Corrector  # noqa: E402
from server.stt.glossary import load_glossary  # noqa: E402


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min", type=int, default=L.MIN_TURNS, help="số lượt dev tối thiểu (mặc định %(default)s)")
    ap.add_argument("--logs", type=Path, default=ROOT / "_local" / "logs")
    args = ap.parse_args()

    turns = L.load_turns(args.logs)
    fixed = [t for t in turns if t.fixed is not None]
    dev = sum(t.split == "dev" for t in fixed)
    print(f"{len(turns)} lượt, {len(fixed)} lượt đã sửa ({dev} dev, {len(fixed) - dev} holdout)")

    decided = L.decided_ids(args.logs.parent / "learning")
    suggestions = L.suggest(turns, load_glossary(), Corrector.load().pairs, decided, args.min)
    if not suggestions:
        print(f"Chưa có đề xuất (cần lỗi lặp ở ≥{args.min} lượt dev).")
    for s in suggestions:
        what = f"+ {s.right}  [{s.topic or '?'}]" if s.kind == "term" else f"{s.wrong} → {s.right}"
        print(f"\n[{s.kind}] {what}  ({len(s.turn_ids)} lượt)")
        if s.warning:
            print(f"  ! {s.warning}")
        for ex in s.examples:
            print(f"  nghe: {ex['heard']}\n  sửa:  {ex['fixed']}")


if __name__ == "__main__":
    main()
