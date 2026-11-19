"""Benchmark TTS: sinh audio cho bench/tts_samples.txt bằng từng engine, đo tốc độ, VRAM, lặng đầu/cuối câu.

Engine lấy từ registry server/tts/engines, tham số từ bảng [tts.<model>] của server/config.toml (cờ dòng lệnh
ghi đè). Bảng có `python` (venv riêng) thì engine chạy trong worker ngoài tiến trình, như trên server:

    .venv/Scripts/python bench/tts_bench.py --model zerotts --gpu --voice maichi
    .venv/Scripts/python bench/tts_bench.py --model vieneu                      # qua worker trong venv vieneu
    .venv/Scripts/python bench/tts_bench.py --model vieneu --opt temperature=0.6 --raw
    .venv/Scripts/python bench/tts_bench.py --compare                           # bảng so sánh
    .venv/Scripts/python bench/tts_bench.py --rate-page                         # trang chấm mù 1-5

Kết quả: _local/tts/<run>/<id>.wav và results.json. Tắt voice server trước khi chạy (GPU).
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.stt_bench import VramMonitor  # noqa: E402
from server.tts.normalize import prepare  # noqa: E402

SAMPLES = ROOT / "bench" / "tts_samples.txt"
OUT_DIR = ROOT / "_local" / "tts"
GAP_S = 0.15   # khoảng lặng giữa hai câu khi ghép, giống lúc phát thật


def load_samples() -> list[dict]:
    text = SAMPLES.read_text(encoding="utf-8")
    out = []
    for block in re.split(r"^=== ", text, flags=re.M)[1:]:
        head, body = block.split("\n", 1)
        sid, group = head.split()[:2]
        out.append({"id": sid, "group": group, "text": body.strip()})
    return out


# ---------- engine ----------

SILENCE = 0.01   # biên độ dưới mức này (~-40 dBFS) coi là lặng


def edge_silence_ms(audio: np.ndarray, sr: int) -> float:
    """Lặng đầu + cuối một câu (ms): số đo cho EngineCaps.native_pause_ms."""
    loud = np.flatnonzero(np.abs(audio) > SILENCE)
    if not len(loud):
        return len(audio) * 1000 / sr
    return (loud[0] + len(audio) - 1 - loud[-1]) * 1000 / sr


def parse_opt(item: str) -> tuple[str, object]:
    """--opt key=value: value đọc như TOML (số, true/false, "chuỗi"), không được thì để chuỗi."""
    import tomllib
    key, _, value = item.partition("=")
    try:
        return key.strip(), tomllib.loads(f"v = {value}")["v"]
    except tomllib.TOMLDecodeError:
        return key.strip(), value


def add_engine_args(ap: argparse.ArgumentParser) -> None:
    """Cờ chọn engine, dùng chung cho các bench TTS (tts_bench, tts_consistency, tts_render)."""
    ap.add_argument("--model", help="engine trong server/tts/engines (zerotts, vieneu, omnivoice) hoặc module:Lớp; "
                                    "mặc định [tts] engine của config")
    ap.add_argument("--voice", help="tên giọng có sẵn của model")
    ap.add_argument("--ref", help="file wav giọng mẫu để clone (omnivoice, vieneu)")
    ap.add_argument("--steps", type=int, help="omnivoice: số bước giải mã")
    ap.add_argument("--threads", type=int, help="zerotts: số luồng CPU cho ONNX")
    ap.add_argument("--device", help="cuda / cpu")
    ap.add_argument("--gpu", action="store_true", help="= --device cuda")
    ap.add_argument("--opt", action="append", default=[], metavar="KEY=VALUE", help="tham số engine bất kỳ, lặp được")
    ap.add_argument("--inproc", action="store_true", help="nạp engine trong tiến trình này, bỏ qua worker")


def engine_options(args, default_device: str | None = None) -> tuple[dict, dict]:
    """(tham số đầy đủ: bảng [tts.<model>] + ghi đè, phần ghi đè từ dòng lệnh để đặt tên run).
    default_device: dùng khi không có --device/--gpu (None = theo config)."""
    import inspect

    from server.config import load_config
    from server.tts.engines import engine_class
    cfg = load_config()
    args.model = args.model or cfg.tts_engine
    options = dict(cfg.tts_engines.get(args.model, {}))
    device = "cuda" if args.gpu else args.device or default_device
    over = {k: v for k, v in (("voice", args.voice), ("ref", args.ref), ("steps", args.steps),
                              ("threads", args.threads), ("device", device)) if v}
    over.update(parse_opt(o) for o in args.opt)
    options.update(over)
    # Giọng mẫu lấy từ tts_samples (vd _local/tts/vieneu/c01.wav) thì có sẵn text, khỏi để model tự nhận dạng
    if options.get("ref") and not options.get("ref_text") \
            and "ref_text" in inspect.signature(engine_class(args.model)).parameters:
        options["ref_text"] = next((s["text"] for s in load_samples() if s["id"] == Path(options["ref"]).stem), "")
    return options, over


def make_engine(args, default_device: str | None = None):
    """Engine theo cờ dòng lệnh. Bảng có `python` thì chạy worker (nhớ gọi close() để tắt)."""
    from server.tts.engines import create_engine
    options, _ = engine_options(args, default_device)
    return create_engine(args.model, options, local=args.inproc)


# ---------- chạy ----------

def synth_sample(model, sample: dict, raw: bool) -> tuple[np.ndarray, float, float, list[float]]:
    """Trả (audio, giây tới mẩu audio đầu, tổng giây tổng hợp, lặng đầu + cuối từng câu ms)."""
    if raw:
        segments = [(sample["text"], "vi")]
    else:
        segments = [(s.text, s.lang) for s in prepare(sample["text"])]
    gap = np.zeros(int(GAP_S * model.sample_rate), dtype=np.float32)
    parts, first, edges = [], None, []
    t0 = time.perf_counter()
    for i, (text, lang) in enumerate(segments):
        if i:
            parts.append(gap)
        sent = []
        for chunk in model.stream(text, lang):
            if first is None:
                first = time.perf_counter() - t0
            sent.append(np.asarray(chunk, dtype=np.float32).reshape(-1))
        if sent:
            edges.append(edge_silence_ms(np.concatenate(sent), model.sample_rate))
        parts += sent
    total = time.perf_counter() - t0
    audio = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
    return audio, first or total, total, edges


def run(args) -> None:
    from server.tts.engines import create_engine
    samples = load_samples()
    if args.only:
        samples = [s for s in samples if s["id"] in args.only.split(",")]
    options, over = engine_options(args)
    voice = over.pop("voice", None)
    run_name = args.name or (args.model + ("-raw" if args.raw else "") + (f"-{voice}" if voice else "")
                             + "".join(f"-{k}{v}" for k, v in sorted(over.items()))
                             + ("-inproc" if args.inproc else ""))
    run_name = re.sub(r"[^\w.-]+", "_", run_name)
    out = OUT_DIR / run_name
    out.mkdir(parents=True, exist_ok=True)

    vram = VramMonitor()
    base = vram.used()
    vram.start()
    t = time.perf_counter()
    model = create_engine(args.model, options, local=args.inproc)
    load_s = time.perf_counter() - t
    try:
        describe = getattr(model, "describe", lambda: "")()
        print(f"Engine {args.model}: {describe}, giọng {model.voice}, {model.sample_rate}Hz")
        print("Giọng có sẵn:", model.voices())
        # Câu khởi động: lần gọi đầu thường chậm (dựng CUDA graph, cache), không tính vào kết quả
        for _ in model.stream("Xin chào, đây là câu khởi động.", "vi"):
            pass

        rows, edges = [], []
        for s in samples:
            audio, ttfa, total, e = synth_sample(model, s, args.raw)
            edges += e
            dur = len(audio) / model.sample_rate
            sf.write(out / f"{s['id']}.wav", audio, model.sample_rate)
            rows.append({"id": s["id"], "group": s["group"], "ttfa": round(ttfa, 3), "synth_s": round(total, 3),
                         "audio_s": round(dur, 2), "rtf": round(total / dur, 3) if dur else None})
            print(f"{s['id']:4} {s['group']:5} TTFA {ttfa * 1000:6.0f}ms  RTF {rows[-1]['rtf']}  {dur:5.1f}s")
        peak = vram.stop()
    finally:
        if hasattr(model, "close"):
            model.close()  # tắt worker

    result = {"run": run_name, "model": args.model, "raw": args.raw, "options": options, "engine": describe,
              "date": datetime.now().isoformat(timespec="seconds"), "sample_rate": model.sample_rate,
              "load_s": round(load_s, 1), "vram_mb": round((peak - base) / 2**20),
              "edge_silence_ms": round(statistics.median(edges)) if edges else None, "items": rows}
    (out / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print_summary([result])


def _summary(r: dict) -> dict:
    items = r["items"]
    rtf = [i["rtf"] for i in items if i["rtf"]]
    ttfa = sorted(i["ttfa"] for i in items)
    return {"run": r["run"], "n": len(items), "rtf": statistics.mean(rtf),
            "ttfa_p50": statistics.median(ttfa), "ttfa_max": ttfa[-1], "vram": r["vram_mb"], "load": r["load_s"],
            "edge": r.get("edge_silence_ms")}


def print_summary(results: list[dict]) -> None:
    print(f"\n{'run':28} {'n':>3} {'RTF':>6} {'TTFA p50':>9} {'TTFA max':>9} {'VRAM MB':>8} {'load s':>7} "
          f"{'lặng ms':>8}")
    for r in results:
        s = _summary(r)
        edge = "" if s["edge"] is None else s["edge"]
        print(f"{s['run']:28} {s['n']:3} {s['rtf']:6.3f} {s['ttfa_p50'] * 1000:7.0f}ms "
              f"{s['ttfa_max'] * 1000:7.0f}ms {s['vram']:8} {s['load']:7} {edge:>8}")


def load_results() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(OUT_DIR.glob("*/results.json"))]


def rate_page() -> None:
    """Trang HTML chấm mù: mỗi đoạn mẫu phát audio các run theo thứ tự ngẫu nhiên, nhãn A/B/C…"""
    results = load_results()
    samples = {s["id"]: s for s in load_samples()}
    rng = random.Random(42)
    blocks, key = [], {}
    for sid, s in samples.items():
        runs = [r["run"] for r in results if (OUT_DIR / r["run"] / f"{sid}.wav").exists()]
        rng.shuffle(runs)
        key[sid] = runs
        players = "".join(
            f'<div class="p"><b>{chr(65 + i)}</b> <audio controls preload="none" src="{run}/{sid}.wav"></audio>'
            f' <select data-sid="{sid}" data-run="{run}"><option></option>'
            + "".join(f"<option>{k}</option>" for k in range(1, 6)) + "</select></div>"
            for i, run in enumerate(runs))
        text = s["text"].replace("&", "&amp;").replace("<", "&lt;")
        blocks.append(f'<section><h3>{sid} · {s["group"]}</h3><pre>{text}</pre>{players}</section>')
    html = f"""<!doctype html><meta charset="utf-8"><title>Chấm mù TTS</title>
<style>body{{font:15px system-ui;max-width:900px;margin:auto;padding:1em}}pre{{white-space:pre-wrap;background:#f4f4f4;padding:.5em}}
.p{{margin:.3em 0}}section{{border-bottom:1px solid #ddd;padding:.5em 0}}</style>
<h1>Chấm mù TTS (1 = tệ, 5 = như người thật)</h1>
<p>Chấm cả độ tự nhiên lẫn đọc đúng thuật ngữ/số. Xong bấm <button id="save">Tải điểm (JSON)</button>
rồi lưu vào _local/tts/ratings.json.</p>
{''.join(blocks)}
<script>
const KEY = "tts-ratings";
const saved = JSON.parse(localStorage.getItem(KEY) || "{{}}");
for (const s of document.querySelectorAll("select")) {{
  s.value = saved[s.dataset.run + "/" + s.dataset.sid] || "";
  s.onchange = () => {{ saved[s.dataset.run + "/" + s.dataset.sid] = s.value; localStorage.setItem(KEY, JSON.stringify(saved)); }};
}}
document.getElementById("save").onclick = () => {{
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(saved, null, 1)], {{type: "application/json"}}));
  a.download = "ratings.json"; a.click();
}};
</script>"""
    (OUT_DIR / "rate.html").write_text(html, encoding="utf-8")
    print(f"Đã tạo {OUT_DIR / 'rate.html'} ({len(results)} run). Mở bằng trình duyệt.")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_engine_args(ap)
    ap.add_argument("--name", help="tên run (mặc định ghép từ model + tham số ghi đè)")
    ap.add_argument("--raw", action="store_true", help="đưa text gốc, không qua server.tts.normalize")
    ap.add_argument("--only", help="chỉ chạy các id, cách nhau bởi dấu phẩy")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--rate-page", action="store_true")
    args = ap.parse_args()
    if args.compare:
        print_summary(load_results())
    elif args.rate_page:
        rate_page()
    elif args.model:
        run(args)
    else:
        ap.error("cần --model, --compare hoặc --rate-page")


if __name__ == "__main__":
    main()
