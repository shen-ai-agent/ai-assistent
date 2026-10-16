"""Benchmark TTS (Phase 3): sinh audio cho bench/tts_samples.txt bằng từng model, đo tốc độ và VRAM.

Mỗi model cài trong venv riêng ở _local/venvs/<model> (dùng chung torch của .venv qua file .pth),
nên chạy script bằng python của venv đó:

    _local/venvs/vieneu/Scripts/python bench/tts_bench.py --model vieneu
    _local/venvs/vieneu/Scripts/python bench/tts_bench.py --model vieneu --raw   # để model tự chuẩn hoá
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


# ---------- adapter cho từng model ----------
# Mỗi adapter có sample_rate và stream(text, lang) -> iterator các mẩu float32.

class VieNeu:
    def __init__(self, args):
        from vieneu import Vieneu
        self.tts = Vieneu("v3turbo")
        self.sample_rate = self.tts.sample_rate
        self.voice = args.voice
        print("Giọng có sẵn:", [v[0] if isinstance(v, tuple) else v for v in self.tts.list_preset_voices()])

    def stream(self, text: str, lang: str):
        yield from self.tts.infer_stream(text, voice=self.voice)


class OmniVoiceVi:
    """OmniVoice fine-tune tiếng Việt. Không stream: cả câu sinh xong mới có audio.
    Không có giọng cố định, nên clone từ file mẫu (mặc định lấy đoạn c01 của run vieneu)."""

    REPO = "splendor1811/omnivoice-vietnamese"

    def __init__(self, args):
        import torch
        from omnivoice import OmniVoice, OmniVoiceGenerationConfig
        self.model = OmniVoice.from_pretrained(self.REPO, device_map="cuda:0", dtype=torch.float16)
        self.sample_rate = self.model.sampling_rate
        ref = Path(args.ref) if args.ref else OUT_DIR / "vieneu" / "c01.wav"
        # File mẫu sinh từ tts_samples thì lấy text theo id; file khác thì để model tự nhận dạng
        ref_text = next((s["text"] for s in load_samples() if s["id"] == ref.stem), None)
        self.prompt = self.model.create_voice_clone_prompt(ref_audio=str(ref), ref_text=ref_text)
        self.config = OmniVoiceGenerationConfig(num_step=args.steps)

    def stream(self, text: str, lang: str):
        yield self.model.generate(text=text, language="vietnamese" if lang == "vi" else "english",
                                  voice_clone_prompt=self.prompt, generation_config=self.config)[0]


class ZeroTTS:
    """ZeroTTS: ONNX chạy CPU, có stream, giọng có sẵn (mặc định maichi)."""

    def __init__(self, args):
        from zerotts import ZeroTTS as Z
        providers = None
        if args.gpu:
            import torch  # noqa: F401  nạp cuDNN/cuBLAS trong torch/lib cho onnxruntime-gpu
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        self.tts = Z.from_pretrained("zeroweight-ai/ZeroTTS", providers=providers, intra_op_num_threads=args.threads)
        print("Provider:", self.tts.prefix_step_sess.get_providers())
        self.sample_rate = self.tts.sample_rate
        self.voice = args.voice or "maichi"
        print("Giọng có sẵn:", self.tts.list_voices())

    def stream(self, text: str, lang: str):
        for chunk in self.tts.synthesize_stream(text, voice=self.voice):
            yield chunk[0]


ADAPTERS = {"vieneu": VieNeu, "omnivoice": OmniVoiceVi, "zerotts": ZeroTTS}


# ---------- chạy ----------

def synth_sample(model, sample: dict, raw: bool) -> tuple[np.ndarray, float, float]:
    """Trả (audio, giây tới mẩu audio đầu, tổng giây tổng hợp)."""
    if raw:
        segments = [(sample["text"], "vi")]
    else:
        segments = [(s.text, s.lang) for s in prepare(sample["text"])]
    gap = np.zeros(int(GAP_S * model.sample_rate), dtype=np.float32)
    parts, first = [], None
    t0 = time.perf_counter()
    for i, (text, lang) in enumerate(segments):
        if i:
            parts.append(gap)
        for chunk in model.stream(text, lang):
            if first is None:
                first = time.perf_counter() - t0
            parts.append(np.asarray(chunk, dtype=np.float32).reshape(-1))
    total = time.perf_counter() - t0
    audio = np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)
    return audio, first or total, total


def run(args) -> None:
    samples = load_samples()
    if args.only:
        samples = [s for s in samples if s["id"] in args.only.split(",")]
    run_name = args.model + ("-raw" if args.raw else "") + (f"-{args.voice}" if args.voice else "")         + (f"-s{args.steps}" if args.model == "omnivoice" else "")         + ((f"-t{args.threads}" + ("-gpu" if args.gpu else "")) if args.model == "zerotts" else "")
    run_name = re.sub(r"[^\w.-]+", "_", run_name)
    out = OUT_DIR / run_name
    out.mkdir(parents=True, exist_ok=True)

    vram = VramMonitor()
    base = vram.used()
    vram.start()
    t = time.perf_counter()
    model = ADAPTERS[args.model](args)
    load_s = time.perf_counter() - t
    # Câu khởi động: lần gọi đầu thường chậm (dựng CUDA graph, cache), không tính vào kết quả
    for _ in model.stream("Xin chào, đây là câu khởi động.", "vi"):
        pass

    rows = []
    for s in samples:
        audio, ttfa, total = synth_sample(model, s, args.raw)
        dur = len(audio) / model.sample_rate
        sf.write(out / f"{s['id']}.wav", audio, model.sample_rate)
        rows.append({"id": s["id"], "group": s["group"], "ttfa": round(ttfa, 3), "synth_s": round(total, 3),
                     "audio_s": round(dur, 2), "rtf": round(total / dur, 3) if dur else None})
        print(f"{s['id']:4} {s['group']:5} TTFA {ttfa * 1000:6.0f}ms  RTF {rows[-1]['rtf']}  {dur:5.1f}s")
    peak = vram.stop()

    result = {"run": run_name, "model": args.model, "raw": args.raw, "voice": args.voice, "ref": args.ref,
              "date": datetime.now().isoformat(timespec="seconds"), "sample_rate": model.sample_rate,
              "load_s": round(load_s, 1), "vram_mb": round((peak - base) / 2**20), "items": rows}
    (out / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print_summary([result])


def _summary(r: dict) -> dict:
    items = r["items"]
    rtf = [i["rtf"] for i in items if i["rtf"]]
    ttfa = sorted(i["ttfa"] for i in items)
    return {"run": r["run"], "n": len(items), "rtf": statistics.mean(rtf),
            "ttfa_p50": statistics.median(ttfa), "ttfa_max": ttfa[-1], "vram": r["vram_mb"], "load": r["load_s"]}


def print_summary(results: list[dict]) -> None:
    print(f"\n{'run':28} {'n':>3} {'RTF':>6} {'TTFA p50':>9} {'TTFA max':>9} {'VRAM MB':>8} {'load s':>7}")
    for r in results:
        s = _summary(r)
        print(f"{s['run']:28} {s['n']:3} {s['rtf']:6.3f} {s['ttfa_p50'] * 1000:7.0f}ms "
              f"{s['ttfa_max'] * 1000:7.0f}ms {s['vram']:8} {s['load']:7}")


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
    ap.add_argument("--model", choices=sorted(ADAPTERS))
    ap.add_argument("--voice", help="tên giọng có sẵn của model")
    ap.add_argument("--ref", help="omnivoice: file wav giọng mẫu để clone (mặc định _local/tts/vieneu/c01.wav)")
    ap.add_argument("--steps", type=int, default=16, help="omnivoice: số bước giải mã")
    ap.add_argument("--threads", type=int, default=4, help="zerotts: số luồng CPU cho ONNX")
    ap.add_argument("--gpu", action="store_true", help="zerotts: chạy ONNX trên CUDA (cần onnxruntime-gpu)")
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
