"""Benchmark STT trên bộ test đã ghi (data/testset).

  python bench/stt_bench.py --config v3_vi v3_vi_prompt
  python bench/stt_bench.py --all
  python bench/stt_bench.py --compare            # bảng so sánh các lần chạy đã lưu
  python bench/stt_bench.py --rescore [--with-corrections]   # chấm lại, không chạy model

Kết quả: _local/bench/stt/<config>/per_utt.csv + summary.json
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import subprocess
import sys
import threading
import time
import tomllib
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import jiwer
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.stt.corrections import Corrector  # noqa: E402
from server.stt.glossary import build_hotwords, build_prompt, detect_topic, load_glossary  # noqa: E402
from server.stt.textnorm import contains_term, normalize  # noqa: E402

TESTSET = ROOT / "data" / "testset"
CONFIGS = ROOT / "bench" / "stt_configs.toml"
OUT_DIR = ROOT / "_local" / "bench" / "stt"
EMPTY = "<empty>"  # jiwer không nhận câu rỗng


class VramMonitor:
    """Lấy mẫu VRAM của GPU bằng NVML (CTranslate2 không cấp phát qua torch)."""

    def __init__(self, interval: float = 0.05):
        import pynvml
        pynvml.nvmlInit()
        self._nvml = pynvml
        self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        self.interval = interval
        self.peak = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def used(self) -> int:
        return self._nvml.nvmlDeviceGetMemoryInfo(self._handle).used

    def start(self):
        self.peak = self.used()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            self.peak = max(self.peak, self.used())
            time.sleep(self.interval)

    def stop(self) -> int:
        self._stop.set()
        if self._thread:
            self._thread.join()
        return self.peak


def load_testset(with_audio: bool = True) -> list[dict]:
    manifest = TESTSET / "transcripts.jsonl"
    if not manifest.exists():
        sys.exit(f"Chưa có bộ test: {manifest}. Ghi bằng bench/record.py trước.")
    items = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    for it in items if with_audio else []:
        audio, sr = sf.read(TESTSET / it["file"], dtype="float32")
        if sr != 16000:
            sys.exit(f"{it['file']}: sample rate {sr}, cần 16000")
        it["audio"] = audio
    return items


def load_configs() -> dict[str, dict]:
    return tomllib.loads(CONFIGS.read_text(encoding="utf-8"))


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def score(rows: list[dict]) -> dict:
    """WER theo corpus + term recall cho một nhóm câu."""
    refs = [r["ref_norm"] or EMPTY for r in rows]
    hyps = [r["hyp_norm"] or EMPTY for r in rows]
    terms_total = sum(len(r["terms"]) for r in rows)
    terms_hit = sum(r["terms_hit"] for r in rows)
    return {
        "n": len(rows),
        "wer": round(jiwer.wer(refs, hyps), 4),
        "term_recall": round(terms_hit / terms_total, 4) if terms_total else None,
        "terms": terms_total,
    }


def summarize(rows: list[dict]) -> dict:
    by = defaultdict(list)
    for r in rows:
        by["all"].append(r)
        by[r["split"]].append(r)
        by[r["group"]].append(r)
        by[f"{r['group']}/{r['split']}"].append(r)
    return {k: score(v) for k, v in sorted(by.items())}


def run_config(name: str, cfg: dict, engine, items: list[dict], glossary, corrector, vram, vram_base) -> dict:
    prompt = build_prompt(glossary, cfg.get("prompt", "none"))
    hotwords = build_hotwords(glossary) if cfg.get("hotwords") else None
    use_corr = cfg.get("corrections", False)
    kwargs = dict(
        language=cfg.get("language", "vi"),
        initial_prompt=prompt,
        hotwords=hotwords,
        beam_size=cfg.get("beam_size", 5),
        en_threshold=cfg.get("en_threshold", 0.8),
    )

    engine.transcribe(items[0]["audio"], **kwargs)  # warm-up, không tính giờ

    # Chọn chủ đề cho prompt = "topic":
    #   oracle    lấy từ đáp án, mô phỏng người dùng tự chọn chủ đề (mức trần)
    #   two_pass  nhận dạng lần 1 bằng prompt mặc định, đoán chủ đề từ kết quả rồi nhận dạng lại
    topic_mode = cfg.get("topic")
    style = cfg.get("prompt", "none")

    rows = []
    vram.start()
    for i, it in enumerate(items, 1):
        t0 = time.perf_counter()
        topic = None
        if topic_mode == "oracle":
            topic = detect_topic(it["text"], glossary)
        if topic_mode == "two_pass":
            res = engine.transcribe(it["audio"], **kwargs)
            topic = detect_topic(res.text, glossary)
        if topic_mode != "two_pass" or topic is not None:  # two_pass không đoán được chủ đề thì giữ lần 1
            res = engine.transcribe(it["audio"], **{**kwargs, "initial_prompt": build_prompt(glossary, style, topic)})
        elapsed = time.perf_counter() - t0
        row = make_row(it, res.text, res.language, res.language_prob, elapsed,
                       corrector if use_corr else None)
        row["topic"] = topic or ""
        rows.append(row)
        print(f"\r  {name}: {i}/{len(items)}", end="", flush=True)
    peak = vram.stop()
    print()

    latencies = [r["latency"] for r in rows]
    total_audio = sum(r["duration"] for r in rows)
    summary = {
        "config": name,
        "settings": {**cfg, "initial_prompt": prompt, "hotwords_text": hotwords},
        "git": git_commit(),
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "rtf": round(sum(latencies) / total_audio, 4),
        "latency_p50": round(statistics.median(latencies), 3),
        "latency_p90": round(statistics.quantiles(latencies, n=10)[-1], 3),
        "vram_gb": round((peak - vram_base) / 2**30, 2),
        "scores": summarize(rows),
    }
    save(name, rows, summary)
    return summary


def make_row(it: dict, raw: str, language: str, lang_prob: float, latency: float, corrector) -> dict:
    hyp = corrector.apply(raw) if corrector else raw
    hyp_norm = normalize(hyp)
    missed = [t for t in it["terms"] if not contains_term(hyp_norm, t)]
    return {
        "id": it["id"], "group": it["group"], "split": it["split"],
        "ref": it["text"], "hyp": hyp, "raw": raw,
        "ref_norm": normalize(it["text"]), "hyp_norm": hyp_norm,
        "terms": it["terms"], "terms_hit": len(it["terms"]) - len(missed), "missed": missed,
        "language": language, "lang_prob": round(float(lang_prob), 3),
        "duration": it["duration"], "latency": round(float(latency), 3),
    }


def rescore(name: str, with_corrections: bool) -> dict:
    """Chấm lại từ transcript thô đã lưu (sau khi đổi cách chuẩn hoá hoặc corrections), không cần GPU."""
    src = OUT_DIR / name
    summary = json.loads((src / "summary.json").read_text(encoding="utf-8"))
    items = {it["id"]: it for it in load_testset(with_audio=False)}
    use_corr = with_corrections or summary["settings"].get("corrections", False)
    corrector = Corrector.load() if use_corr else None
    with open(src / "per_utt.csv", encoding="utf-8-sig") as f:
        rows = [make_row(items[r["id"]], r["raw"], r["language"], r["lang_prob"], r["latency"], corrector)
                for r in csv.DictReader(f)]
    out_name = f"{name}+corr" if with_corrections and not name.endswith("+corr") else name
    summary.update(config=out_name, scores=summarize(rows),
                   rescored=datetime.now().isoformat(timespec="seconds"))
    summary["settings"]["corrections"] = use_corr
    save(out_name, rows, summary)
    return summary


def save(name: str, rows: list[dict], summary: dict):
    out = OUT_DIR / name
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "per_utt.csv", "w", encoding="utf-8-sig", newline="") as f:
        fields = ["id", "group", "split", "topic", "language", "lang_prob", "latency", "terms_hit",
                  "missed", "ref", "hyp", "raw", "ref_norm", "hyp_norm"]
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({**r, "missed": ";".join(r["missed"])})
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def pct(v) -> str:
    return "  -  " if v is None else f"{v * 100:5.1f}"


def print_table(summaries: list[dict]):
    cols = [("WER", "all", "wer"), ("WER vi", "vi", "wer"), ("WER mix", "mix", "wer"),
            ("WER dense", "dense", "wer"), ("WER en", "en", "wer"),
            ("Term dev", "dev", "term_recall"), ("Term hold", "holdout", "term_recall"),
            ("Term mix/h", "mix/holdout", "term_recall")]
    head = f"{'config':24}" + "".join(f"{c[0]:>11}" for c in cols) + f"{'RTF':>7}{'p90 s':>7}{'VRAM':>6}"
    print(head)
    print("-" * len(head))
    for s in summaries:
        sc = s["scores"]
        line = f"{s['config']:24}" + "".join(f"{pct(sc.get(k, {}).get(m)):>11}" for _, k, m in cols)
        line += f"{s['rtf']:7.3f}{s['latency_p90']:7.2f}{s['vram_gb']:6.1f}"
        print(line)
    print("(WER, term recall tính bằng %; VRAM tính bằng GB, đã trừ mức nền)")


def compare(names: list[str] | None):
    files = sorted(OUT_DIR.glob("*/summary.json"))
    summaries = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    if names:
        summaries = [s for s in summaries if s["config"] in names]
    if not summaries:
        sys.exit("Chưa có kết quả nào.")
    print_table(summaries)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", nargs="+", help="tên cấu hình trong stt_configs.toml")
    ap.add_argument("--all", action="store_true", help="chạy mọi cấu hình")
    ap.add_argument("--limit", type=int, help="chỉ chạy N câu đầu (thử nhanh)")
    ap.add_argument("--compare", action="store_true", help="in bảng so sánh các kết quả đã lưu")
    ap.add_argument("--rescore", action="store_true",
                    help="chấm lại kết quả đã lưu của các --config (hoặc tất cả), không chạy model")
    ap.add_argument("--with-corrections", action="store_true",
                    help="(với --rescore) áp dụng data/corrections.tsv, lưu thành <config>+corr")
    ap.add_argument("--testset", type=Path,
                    help="thư mục bộ test khác (vd _local/personal); kết quả lưu ở _local/bench/stt-<tên thư mục>")
    args = ap.parse_args()

    if args.testset:
        global TESTSET, OUT_DIR
        TESTSET = args.testset.resolve()
        OUT_DIR = ROOT / "_local" / "bench" / f"stt-{TESTSET.name}"

    if args.rescore:
        names = args.config or [p.parent.name for p in sorted(OUT_DIR.glob("*/summary.json"))
                                if not p.parent.name.endswith("+corr")]
        print_table([rescore(n, args.with_corrections) for n in names])
        return

    if args.compare:
        compare(args.config)
        return

    configs = load_configs()
    names = list(configs) if args.all else (args.config or [])
    if not names:
        ap.error("cần --config hoặc --all")
    unknown = [n for n in names if n not in configs]
    if unknown:
        ap.error(f"không có cấu hình: {', '.join(unknown)}")

    items = load_testset()
    if args.limit:
        items = items[: args.limit]
    glossary = load_glossary()
    corrector = Corrector.load()
    vram = VramMonitor()
    print(f"Bộ test: {len(items)} câu, {sum(i['duration'] for i in items) / 60:.1f} phút")

    # Gom theo model để mỗi model chỉ load một lần
    by_model = defaultdict(list)
    for n in names:
        c = configs[n]
        by_model[(c["model"], c.get("compute_type", "int8_float16"))].append(n)

    from server.stt.engine import WhisperEngine  # import chậm (ctranslate2)

    summaries = []
    for (model, compute_type), group in by_model.items():
        base = vram.used()
        print(f"Load {model} ({compute_type})...")
        engine = WhisperEngine(model, compute_type=compute_type)
        for n in group:
            summaries.append(run_config(n, configs[n], engine, items, glossary, corrector, vram, base))
        del engine

    print()
    print_table(summaries)


if __name__ == "__main__":
    main()
