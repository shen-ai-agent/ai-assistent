"""ZeroTTS bớt phụ thuộc CPU khi chạy GPU, để máy đang bận nhiều app thì giọng đọc vẫn không giật.

Mỗi frame (80ms audio) ZeroTTS gọi `local_frame_decode` (~2.500 kernel GPU tí hon) và `prefix_step`. Thời gian
nằm ở việc CPU gửi từng kernel, không phải ở GPU tính, nên CPU bận thì RTF vọt lên (đo 0.7 → 1.2 khi chạy cùng
các dev server). Hai cách sửa, đều cho ra đúng từng mẫu audio như bản gốc (tests/test_tts_gpu.py):

1. CUDA Graph cho `local_frame_decode`: ghi chuỗi kernel một lần, sau đó mỗi frame phát lại bằng một lệnh.
   Graph gốc không ghi được vì (a) ArgMax nhận INT64 (lấy mẫu bằng `cumsum > u` → Cast INT64 → ArgMax), CUDA EP
   không có kernel đó nên 17 lần mỗi frame phải về CPU; (b) `k` của TopK là đầu vào nên mọi shape phía sau tính
   lúc chạy trên CPU. `build_graph_model` sửa một bản riêng trong models/zerotts-graph/: Cast sang FLOAT (ArgMax
   trên 0/1 vẫn ra vị trí True đầu tiên), đóng cứng text_topk/audio_topk thành hằng số và batch = 1. Tham số khác
   giá trị đã đóng cứng (hoặc cfg_scale > 1) thì quay về session gốc.
2. IOBinding: KV cache, cross-attention của text và state codec stream (~25MB mỗi mẩu) nằm luôn trên GPU, đầu ra
   lượt trước nối thẳng vào đầu vào lượt sau; chỉ hidden, mã âm và audio về CPU. `_generate_frames` của ZeroTTS
   chỉ chuyển packed_kv / full_valid / cross_kv / text_valid giữa hai hàm `_prefix_step_*`, không đọc nội dung,
   nên thay hai hàm đó là đủ.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
from zerotts import ZeroTTS
from zerotts.codec import MossCodecDecoder

log = logging.getLogger("tts")

GRAPH_DIR = Path(__file__).resolve().parents[2] / "models" / "zerotts-graph"
GRAPH_TOPK = {"text_topk": 50, "audio_topk": 25}  # mặc định của ZeroTTS.synthesize_stream
_GRAPH_VERSION = 1  # tăng khi đổi cách sửa graph, để bản cũ trong models/ được tạo lại

_NP_TYPES = {"tensor(float)": np.float32, "tensor(bool)": np.bool_, "tensor(int64)": np.int64,
             "tensor(int32)": np.int32}


def _np_in(io, name: str, arr: np.ndarray):
    io.bind_cpu_input(name, np.ascontiguousarray(arr))


def build_graph_model(src: Path, dst_dir: Path = GRAPH_DIR) -> Path:
    """Bản local_frame_decode ghi được CUDA Graph. Tạo một lần (~10 giây), tạo lại khi file gốc đổi."""
    dst, meta_path = dst_dir / "local_frame_decode.onnx", dst_dir / "meta.json"
    stat = src.stat()
    meta = {"version": _GRAPH_VERSION, "src": str(src), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            **GRAPH_TOPK}
    if dst.exists() and meta_path.exists() and json.loads(meta_path.read_text(encoding="utf-8")) == meta:
        return dst

    import onnx  # chỉ cần lúc tạo
    m = onnx.load(str(src))
    g = m.graph
    consumers: dict[str, list] = {}
    for n in g.node:
        for i in n.input:
            consumers.setdefault(i, []).append(n)
    casts = 0
    for n in g.node:
        outs = consumers.get(n.output[0], []) if n.op_type == "Cast" else []
        if outs and all(c.op_type == "ArgMax" for c in outs):
            for a in n.attribute:
                if a.name == "to" and a.i == onnx.TensorProto.INT64:
                    a.i = onnx.TensorProto.FLOAT
                    casts += 1
    keep = [i for i in g.input if i.name not in GRAPH_TOPK]
    if casts == 0 or len(keep) != len(g.input) - len(GRAPH_TOPK):
        raise RuntimeError(f"graph local_frame_decode khác dự kiến (Cast→ArgMax: {casts}), không sửa")
    for name, v in GRAPH_TOPK.items():
        g.initializer.append(onnx.helper.make_tensor(name, onnx.TensorProto.INT64, [1], [v]))
    del g.input[:]
    g.input.extend(keep)
    for i in g.input:
        for dim in i.type.tensor_type.shape.dim:
            if dim.dim_param == "B":
                dim.Clear()
                dim.dim_value = 1
    dst_dir.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    onnx.save(m, str(tmp))
    tmp.replace(dst)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Đã tạo %s (%d Cast→ArgMax đổi sang FLOAT, topk %s)", dst, casts, GRAPH_TOPK)
    return dst


class _LocalDecodeGraph:
    """local_frame_decode chạy bằng CUDA Graph: đầu vào/ra là bộ đệm GPU cố định, mỗi lượt chép giá trị vào rồi phát lại."""

    def __init__(self, path: Path, sess_options, device_id: int = 0):
        import onnxruntime as ort
        self.sess = ort.InferenceSession(
            str(path), sess_options=sess_options,
            providers=[("CUDAExecutionProvider", {"enable_cuda_graph": "1", "device_id": str(device_id)}),
                       "CPUExecutionProvider"])
        self.inputs = {}
        for i in self.sess.get_inputs():
            self.inputs[i.name] = ort.OrtValue.ortvalue_from_numpy(
                np.zeros(i.shape, dtype=_NP_TYPES[i.type]), "cuda", device_id)
        self.is_eoa = ort.OrtValue.ortvalue_from_shape_and_type([1], np.int32, "cuda", device_id)
        self.codes = None
        self.io = self.sess.io_binding()
        for name, v in self.inputs.items():
            self.io.bind_ortvalue_input(name, v)
        self.io.bind_ortvalue_output("is_eoa", self.is_eoa)

    def bind_codes(self, num_codebooks: int, device_id: int = 0):
        import onnxruntime as ort
        self.codes = ort.OrtValue.ortvalue_from_shape_and_type([1, num_codebooks], np.int32, "cuda", device_id)
        self.io.bind_ortvalue_output("codes", self.codes)

    def run(self, feeds: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
        for name, v in self.inputs.items():
            v.update_inplace(np.ascontiguousarray(feeds[name]))
        self.sess.run_with_iobinding(self.io)
        return self.is_eoa.numpy(), self.codes.numpy()


class ZeroTtsGpu(ZeroTTS):
    DEVICE = "cuda"

    def __init__(self, model_dir, cuda_graph: bool = True, **kwargs):
        warmup = kwargs.pop("warmup", True)
        super().__init__(model_dir, warmup=False, **kwargs)
        self.codec = _GpuCodec(self.codec, self.DEVICE)
        self.graph: _LocalDecodeGraph | None = None
        if cuda_graph:
            try:
                import onnxruntime as ort
                so = ort.SessionOptions()
                so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                so.intra_op_num_threads = kwargs.get("intra_op_num_threads", 4)
                so.inter_op_num_threads = 1
                path = build_graph_model(Path(model_dir) / "onnx" / "local_frame_decode.onnx")
                self.graph = _LocalDecodeGraph(path, so)
                self.graph.bind_codes(self.num_codebooks)
            except Exception as e:  # thiếu gói onnx, graph khác dự kiến, CUDA EP không ghi được...: chạy như cũ
                log.warning("ZeroTTS không dùng được CUDA Graph (%s), chạy bình thường", e)
                self.graph = None
        if warmup:
            self.warmup()  # lượt đầu của CUDA Graph là lượt ghi, để nó rơi vào lúc khởi động

    # ── local_frame_decode ───────────────────────────────────────────────────

    def _local_decode_frame(self, h, forbid_eoa, text_temperature, text_topk, audio_temperature, audio_topk,
                            audio_topp, audio_repetition_penalty, seen_mask, cfg_scale=1.0):
        if (self.graph is None or h.shape[0] != 1 or text_topk != GRAPH_TOPK["text_topk"]
                or audio_topk != GRAPH_TOPK["audio_topk"]):
            return super()._local_decode_frame(h, forbid_eoa, text_temperature, text_topk, audio_temperature,
                                               audio_topk, audio_topp, audio_repetition_penalty, seen_mask,
                                               cfg_scale)
        K = self.num_codebooks
        # Thứ tự lấy số ngẫu nhiên giữ như bản gốc (ctrl rồi audio), để cùng seed thì cùng audio
        ctrl_u = np.random.random(1).astype(np.float32)
        audio_u = np.random.random((1, K)).astype(np.float32)
        is_eoa, codes = self.graph.run({
            "global_hidden": h.astype(np.float32, copy=False),
            "forbid_eoa": np.array([bool(forbid_eoa)], dtype=bool),
            "text_temperature": np.array([text_temperature], dtype=np.float32),
            "audio_temperature": np.array([audio_temperature], dtype=np.float32),
            "audio_topp": np.array([audio_topp], dtype=np.float32),
            "audio_repetition_penalty": np.array([audio_repetition_penalty], dtype=np.float32),
            "seen_mask": seen_mask,
            "ctrl_random_u": ctrl_u,
            "audio_random_u": audio_u,
            "cfg_scale": np.array([cfg_scale], dtype=np.float32),
        })
        codes = codes.astype(np.int64)
        for c in range(K):
            seen_mask[0, c, codes[0, c]] = True
        return (self.eoa_id if bool(is_eoa.reshape(-1)[0]) else self.slot_id), codes

    # ── prefix_step ──────────────────────────────────────────────────────────

    def _bind_out(self, io, sess, on_cpu: set[str]):
        for o in sess.get_outputs():
            io.bind_output(o.name, "cpu" if o.name in on_cpu else self.DEVICE)

    def _prefix_step_init(self, text_ids: np.ndarray, txt_lengths: np.ndarray, voice_emb: np.ndarray):
        B, V, L = voice_emb.shape[0], voice_emb.shape[1], text_ids.shape[1]
        if text_ids.shape[0] != B:
            text_ids = np.broadcast_to(text_ids, (B, L))
            txt_lengths = np.broadcast_to(txt_lengths, (B,))

        io = self.text_encoder_sess.io_binding()
        _np_in(io, "text_ids", text_ids)
        _np_in(io, "txt_lengths", txt_lengths)
        self._bind_out(io, self.text_encoder_sess, {"soa_embed"})
        self.text_encoder_sess.run_with_iobinding(io)
        enc = dict(zip([o.name for o in self.text_encoder_sess.get_outputs()], io.get_outputs()))
        soa_embed = enc["soa_embed"].numpy()
        cross_kv, text_valid = enc["cross_kv"], enc["text_valid"]

        external_embed = np.concatenate([voice_emb.astype(np.float32), soa_embed.astype(np.float32)], axis=1)
        T = V + 1
        hidden, packed_kv, full_valid = self._prefix_run(
            {
                "external_embed": external_embed,
                "use_external_embed": np.ones((B, T), dtype=bool),
                "frame_codes": np.zeros((B, T, self.num_codebooks), dtype=np.int64),
                "new_pos": np.tile(np.arange(T, dtype=np.int64), (B, 1)),
                "new_valid": np.ones((B, T), dtype=bool),
                "new_bidirectional": np.concatenate([np.ones((B, V), dtype=bool), np.zeros((B, 1), dtype=bool)],
                                                    axis=1),
                "past_valid": np.zeros((B, 0), dtype=bool),
                "packed_kv": np.zeros((self.n_layers, 2, B, self.n_heads, 0, self.d_head), dtype=np.float32),
            },
            cross_kv, text_valid)
        return hidden[:, -1, :], packed_kv, full_valid, (B, cross_kv), text_valid

    def _prefix_step_frame(self, frame_codes: np.ndarray, frame_index: np.ndarray, packed_kv, full_valid,
                           n_voice: int = 0, cross_kv=None, text_valid=None):
        B, cross = cross_kv  # batch đi kèm cross_kv vì packed_kv là OrtValue, không có .shape như numpy
        if frame_codes.shape[0] != B:
            frame_codes = np.broadcast_to(frame_codes, (B,) + frame_codes.shape[1:])
        new_pos = np.tile((n_voice + 1 + frame_index).astype(np.int64).reshape(1, 1), (B, 1))
        hidden, packed_kv, full_valid = self._prefix_run(
            {
                "external_embed": np.zeros((B, 1, self.d_model), dtype=np.float32),
                "use_external_embed": np.zeros((B, 1), dtype=bool),
                "frame_codes": frame_codes,
                "new_pos": new_pos,
                "new_valid": np.ones((B, 1), dtype=bool),
                "new_bidirectional": np.zeros((B, 1), dtype=bool),
                "packed_kv": packed_kv,
                "past_valid": full_valid,
            },
            cross, text_valid)
        return hidden[:, -1, :], packed_kv, full_valid

    def _prefix_run(self, feeds: dict, cross_kv, text_valid):
        """Một lượt prefix_step: numpy thì chép lên, OrtValue (đầu ra lượt trước) thì dùng thẳng trên GPU."""
        sess = self.prefix_step_sess
        io = sess.io_binding()
        for name, v in {**feeds, "cross_kv": cross_kv, "text_valid": text_valid}.items():
            if isinstance(v, np.ndarray):
                _np_in(io, name, v)
            else:
                io.bind_ortvalue_input(name, v)
        self._bind_out(io, sess, {"hidden"})
        sess.run_with_iobinding(io)
        out = dict(zip([o.name for o in sess.get_outputs()], io.get_outputs()))
        return out["hidden"].numpy(), out["new_packed_kv"], out["full_valid"]


class _GpuCodec:
    """Thay MossCodecDecoder: decode (cả câu) giữ nguyên, streaming_decoder giữ state trên GPU."""

    def __init__(self, codec: MossCodecDecoder, device: str):
        self._codec = codec
        self.device = device
        self.sample_rate = codec.sample_rate
        streaming = codec._meta.get("streaming_decode", {})
        self._pairs: list[tuple[str, str]] = []  # (tên đầu vào state, tên đầu ra tương ứng)
        init: dict[str, np.ndarray] = {}
        for spec in streaming.get("transformer_offsets", []):
            self._pairs.append((spec["input_name"], spec["output_name"]))
            init[spec["input_name"]] = np.zeros(tuple(spec["shape"]), dtype=np.int32)
        for spec in streaming.get("attention_caches", []):
            for key in ("offset", "cached_keys", "cached_values", "cached_positions"):
                self._pairs.append((spec[f"{key}_input_name"], spec[f"{key}_output_name"]))
            init[spec["offset_input_name"]] = np.zeros(tuple(spec["offset_shape"]), dtype=np.int32)
            init[spec["cached_keys_input_name"]] = np.zeros(tuple(spec["cache_shape"]), dtype=np.float32)
            init[spec["cached_values_input_name"]] = np.zeros(tuple(spec["cache_shape"]), dtype=np.float32)
            # -1: vị trí 0 là vị trí thật (giống MossStreamingDecoder._reset_state)
            init[spec["cached_positions_input_name"]] = np.full(tuple(spec["positions_shape"]), -1, dtype=np.int32)
        from onnxruntime import OrtValue
        # State ban đầu chỉ làm đầu vào (onnxruntime không ghi vào đầu vào), nên mọi stream dùng chung được
        self._init = {k: OrtValue.ortvalue_from_numpy(v, device, 0) for k, v in init.items()}

    def decode(self, codes_bkt: np.ndarray) -> np.ndarray:
        return self._codec.decode(codes_bkt)

    def streaming_decoder(self) -> _GpuStreamingDecoder:
        return _GpuStreamingDecoder(self)


class _GpuStreamingDecoder:
    def __init__(self, codec: _GpuCodec):
        self._sess = codec._codec._decode_step_sess
        self._device = codec.device
        self._pairs = codec._pairs
        self._state_outs = {out for _, out in self._pairs}
        self._out_names = [o.name for o in self._sess.get_outputs()]
        self._state = dict(codec._init)

    def decode_chunk(self, codes_bkt: np.ndarray) -> np.ndarray:
        codes = np.asarray(codes_bkt)
        if codes.ndim == 2:
            codes = codes[None, :, :]
        codes_btk = codes.transpose(0, 2, 1).astype(np.int32)  # (1, K, n) -> (1, n, K)
        io = self._sess.io_binding()
        _np_in(io, "audio_codes", codes_btk)
        _np_in(io, "audio_code_lengths", np.array([codes_btk.shape[1]], dtype=np.int32))
        for name, v in self._state.items():
            io.bind_ortvalue_input(name, v)
        for name in self._out_names:
            io.bind_output(name, self._device if name in self._state_outs else "cpu")
        self._sess.run_with_iobinding(io)
        named = dict(zip(self._out_names, io.get_outputs()))
        self._state = {inp: named[out] for inp, out in self._pairs}
        audio = named["audio"].numpy()
        n = int(named["audio_lengths"].numpy().reshape(-1)[0])
        return audio[:, :, :n].mean(axis=1).astype(np.float32)

    def close(self) -> None:
        self._state = {}
