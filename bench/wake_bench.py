"""Đo nhận tên gọi trợ lý (T-010) offline, đúng đường đi của server lúc SLEEP:
Silero VAD streaming → Segmenter → Whisper không prompt (wake_window_s đầu đoạn) → WakeMatcher → decide.

Dữ liệu (ghi bằng bench/record.py):
  data/wake_testset/        câu trong data/wake_phrases.tsv. name → phải "ack", ask → "ask", near → "ignore"
  data/testset/             100 câu STT, không có tên → mọi đoạn phải "ignore"
  _local/wake_ambient/*.wav âm nền dài → số lần nhận nhầm mỗi giờ

    python bench/wake_bench.py                       # chạy với tham số trong server/config.toml
    python bench/wake_bench.py --name s075 --min-score 0.75
    python bench/wake_bench.py --compare

Tắt server trước khi chạy (cùng dùng GPU). Kết quả: _local/bench/wake/<name>/per_utt.csv + summary.json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.assistant import Segmenter, StreamingVad, WakeMatcher  # noqa: E402
from server.assistant.wake import decide  # noqa: E402
from server.config import load_config  # noqa: E402
from stt_bench import VramMonitor, git_commit  # noqa: E402

WAKE_SET = ROOT / "data" / "wake_testset"
STT_SET = ROOT / "data" / "testset"
AMBIENT = ROOT / "_local" / "wake_ambient"
OUT_DIR = ROOT / "_local" / "bench" / "wake"
SR = 16000
EXPECT = {"name": "ack", "ask": "ask", "near": "ignore", "stt": "ignore"}
CHUNK = 1600  # 100ms, như frame của trình duyệt


def load_manifest(folder: Path, group: str | None = None) -> list[dict]:
    manifest = folder / "transcripts.jsonl"
    if not manifest.exists():
        return []
    items = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    for it in items:
        it["path"] = folder / it["file"]
        if group:
            it["group"] = group
    return items


def read16k(path: Path) -> np.ndarray:
    audio, sr = sf.read(path, dtype="float32")
    if sr != SR:
        sys.exit(f"{path.name}: sample rate {sr}, cần {SR}")
    return audio if audio.ndim == 1 else audio[:, 0]


class Runner:
    def __init__(self, stt, vad_session, cfg, args):
        self.stt, self.vad_session = stt, vad_session
        self.silence_ms = args.silence_ms or cfg.assistant_sleep_silence_ms
        self.window_ms = round((args.window_s or cfg.assistant_wake_window_s) * 1000)
        self.min_rest = cfg.assistant_min_rest_words
        self.grace_ms = cfg.assistant_ack_grace_ms
        self.prompt = cfg.assistant_wake_prompt if args.prompt is None else args.prompt
        self.max_s = cfg.assistant_max_segment_s
        self.matcher = WakeMatcher(cfg.assistant_name, cfg.assistant_aliases,
                                   min_score=args.min_score or cfg.assistant_wake_min_score)

    def segments(self, audio: np.ndarray):
        """Cắt đoạn như server: đẩy từng frame 100ms, thêm im lặng để đoạn cuối kịp kết thúc.
        Trả (đoạn, vị trí mic lúc cắt xong)."""
        seg = Segmenter(StreamingVad(self.vad_session), silence_ms=self.silence_ms, max_s=self.max_s)
        tail = np.zeros(round((self.silence_ms + 500) * SR / 1000), dtype=np.float32)
        audio = np.concatenate([np.zeros(SR // 2, dtype=np.float32), audio, tail])
        for i in range(0, len(audio), CHUNK):
            for kind, value in seg.push(audio[i:i + CHUNK]):
                if kind == "end":
                    yield value, seg.pos

    def judge(self, s):
        text, stt_ms = self.stt.transcribe_plain(s.audio[: self.window_ms * SR // 1000], self.prompt)
        m = self.matcher.match(text)
        return decide(m, s.audio_ms, self.window_ms, self.min_rest), m, text, stt_ms


def run_clips(runner: Runner, items: list[dict]) -> list[dict]:
    rows = []
    for n, it in enumerate(items, 1):
        segs = list(runner.segments(read16k(it["path"])))
        judged = [runner.judge(s) for s, _ in segs]
        # Cả câu một lượt: trợ lý phản ứng với đoạn đầu tiên có tên; không có thì coi là bỏ qua
        i = next((k for k, j in enumerate(judged) if j[0] != "ignore"), None)
        hit = judged[i] if i is not None else None
        action = hit[0] if hit else "ignore"
        if action == "ack" and i + 1 < len(segs):
            # Như HandsFree._grace: đoạn sau bắt đầu trước lúc kịp đáp (nhận dạng + ack_grace_ms) là câu hỏi
            ack_from = segs[i][1] + (hit[3] + runner.grace_ms) * SR // 1000
            if segs[i + 1][0].start < ack_from:
                action = "ask"
        expect = EXPECT[it["group"]]
        rows.append({
            "id": it["id"], "group": it["group"], "split": it.get("split", ""), "text": it["text"],
            "expect": expect, "action": action, "ok": action == expect,
            "found": hit is not None, "segments": len(segs),
            "heard": " | ".join(j[2] for j in judged), "score": max((j[1].score for j in judged), default=0.0),
            "stt_ms": max((j[3] for j in judged), default=0),
        })
        if n % 20 == 0:
            print(f"  {n}/{len(items)}")
    return rows


def run_ambient(runner: Runner, files: list[Path]) -> tuple[float, list[dict]]:
    hours, hits = 0.0, []
    for f in files:
        audio = read16k(f)
        hours += len(audio) / SR / 3600
        print(f"  {f.name}: {len(audio) / SR / 60:.1f} phút")
        for s, _ in runner.segments(audio):
            action, m, text, stt_ms = runner.judge(s)
            if action != "ignore":
                hits.append({"file": f.name, "at_s": round(s.start / SR, 1), "action": action,
                             "heard": text, "score": m.score})
    return hours, hits


def pct(v):
    return "-" if v is None else f"{v * 100:.0f}%"


def summarize(rows: list[dict], hours: float, ambient_hits: list[dict], stt_all: list[int]) -> dict:
    out = {"by": {}}
    keys = defaultdict(list)
    for r in rows:
        keys[(r["group"], r["split"])].append(r)
        keys[(r["group"], "all")].append(r)
    for (g, sp), rs in sorted(keys.items()):
        out["by"][f"{g}/{sp}"] = {
            "n": len(rs), "accuracy": sum(r["ok"] for r in rs) / len(rs),
            "found": sum(r["found"] for r in rs) / len(rs),
            "split_mid": sum(r["segments"] > 1 for r in rs) / len(rs),
        }
    pos = [r for r in rows if r["group"] in ("name", "ask")]
    neg = [r for r in rows if r["group"] in ("near", "stt")]
    out["recall"] = sum(r["found"] for r in pos) / len(pos) if pos else None
    out["false_accept"] = sum(r["found"] for r in neg) / len(neg) if neg else None
    out["ambient_hours"] = round(hours, 3)
    out["ambient_fa"] = len(ambient_hits)
    out["ambient_fa_per_hour"] = len(ambient_hits) / hours if hours else None
    stt = sorted(stt_all)
    out["stt_ms_p50"] = stt[len(stt) // 2] if stt else None
    out["stt_ms_p95"] = stt[min(len(stt) - 1, int(len(stt) * 0.95))] if stt else None
    return out


def print_summary(name: str, s: dict):
    p = s["params"]
    print(f"\n=== {name}  (min_score {p['min_score']}, silence {p['silence_ms']}ms, cửa sổ {p['window_ms']}ms)")
    print(f"{'nhóm/split':<16}{'n':>4}{'đúng':>7}{'có tên':>8}{'bị cắt':>8}")
    for k, v in s["by"].items():
        print(f"{k:<16}{v['n']:>4}{pct(v['accuracy']):>7}{pct(v['found']):>8}{pct(v['split_mid']):>8}")
    fa_h = s["ambient_fa_per_hour"]
    print(f"recall {pct(s['recall'])} · nhận nhầm {pct(s['false_accept'])} · âm nền {s['ambient_fa']} lần / "
          f"{s['ambient_hours']:.2f}h ({'-' if fa_h is None else f'{fa_h:.1f}/h'}) · "
          f"STT p50 {s['stt_ms_p50']}ms p95 {s['stt_ms_p95']}ms · VRAM {s['vram_mb']}MB")


def compare():
    runs = sorted(OUT_DIR.glob("*/summary.json"))
    if not runs:
        sys.exit("Chưa có kết quả nào.")
    print(f"{'run':<18}{'score':>6}{'sil':>6}{'recall':>8}{'FA':>6}{'FA/h':>7}{'ask':>6}{'ack':>6}{'p50':>7}{'p95':>7}")
    for f in runs:
        s = json.loads(f.read_text(encoding="utf-8"))
        p, by = s["params"], s["by"]
        fa_h = s["ambient_fa_per_hour"]
        print(f"{f.parent.name:<18}{p['min_score']:>6}{p['silence_ms']:>6}{pct(s['recall']):>8}"
              f"{pct(s['false_accept']):>6}{'-' if fa_h is None else f'{fa_h:.1f}':>7}"
              f"{pct(by.get('ask/all', {}).get('accuracy')):>6}{pct(by.get('name/all', {}).get('accuracy')):>6}"
              f"{s['stt_ms_p50'] or '-':>7}{s['stt_ms_p95'] or '-':>7}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="default", help="tên lần chạy (thư mục kết quả)")
    ap.add_argument("--min-score", type=float, help="ghi đè assistant.wake_min_score")
    ap.add_argument("--silence-ms", type=int, help="ghi đè assistant.sleep_silence_ms")
    ap.add_argument("--prompt", help="ghi đè assistant.wake_prompt (\"\" = không prompt)")
    ap.add_argument("--window-s", type=float, help="ghi đè assistant.wake_window_s")
    ap.add_argument("--no-stt-set", action="store_true", help="bỏ 100 câu data/testset (âm tính)")
    ap.add_argument("--compare", action="store_true", help="bảng so sánh các lần chạy đã lưu")
    args = ap.parse_args()
    if args.compare:
        return compare()

    cfg = load_config()
    items = load_manifest(WAKE_SET)
    if not items:
        print("⚠ Chưa có bộ gọi tên, chỉ đo nhận nhầm. Ghi bằng: python bench/record.py "
              f"--sentences data/wake_phrases.tsv --out {WAKE_SET.relative_to(ROOT).as_posix()}")
    if not args.no_stt_set:
        items += load_manifest(STT_SET, group="stt")
    if not items:
        sys.exit("Không có câu nào để đo.")
    ambient = sorted(AMBIENT.glob("*.wav"))
    print(f"Bộ gọi tên + âm tính: {len(items)} câu · âm nền: {len(ambient)} file")

    from faster_whisper.vad import get_vad_model
    from server.stt.engine import WhisperEngine  # import chậm (ctranslate2)
    from server.stt.service import SttService

    vram = VramMonitor()
    base = vram.used()
    vram.start()
    engine = WhisperEngine(cfg.model, cfg.compute_type, cfg.device)
    stt = SttService(engine, language=cfg.language, beam_size=cfg.beam_size)
    stt.transcribe_plain(np.zeros(SR, dtype=np.float32))  # warm-up
    runner = Runner(stt, get_vad_model().session, cfg, args)

    t0 = time.perf_counter()
    rows = run_clips(runner, items)
    hours, hits = run_ambient(runner, ambient)
    stt_all = [r["stt_ms"] for r in rows if r["segments"]]
    summary = summarize(rows, hours, hits, stt_all)
    summary.update({
        "name": args.name, "commit": git_commit(), "at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_s": round(time.perf_counter() - t0, 1), "vram_mb": round((vram.stop() - base) / 2**20),
        "params": {"min_score": runner.matcher.min_score, "silence_ms": runner.silence_ms,
                   "window_ms": runner.window_ms, "min_rest_words": runner.min_rest, "ack_grace_ms": runner.grace_ms, "prompt": runner.prompt,
                   "name": cfg.assistant_name, "aliases": cfg.assistant_aliases, "model": cfg.model},
        "ambient_hits": hits,
    })

    out = OUT_DIR / args.name
    out.mkdir(parents=True, exist_ok=True)
    with (out / "per_utt.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print_summary(args.name, summary)
    wrong = [r for r in rows if not r["ok"] and r["split"] != "holdout"]
    if wrong:
        print("\nSai ở dev (holdout không in, chỉ để kiểm chứng):")
        for r in wrong:
            print(f"  {r['id']:<10} cần {r['expect']:<6} → {r['action']:<6} nghe: {r['heard']}")
    print(f"\n→ {out.relative_to(ROOT).as_posix()}")


if __name__ == "__main__":
    main()
