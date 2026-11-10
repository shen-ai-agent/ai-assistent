"""server/tts/zerotts_gpu.py: sửa graph cho CUDA Graph (không cần GPU) và so audio với ZeroTTS gốc (cần GPU).

Phần so audio nạp model thật lên GPU (~1 phút), chỉ chạy khi đặt GPU_TESTS=1 và đã tắt server:
    GPU_TESTS=1 .venv/Scripts/python -m pytest -q tests/test_tts_gpu.py
"""

from __future__ import annotations

import json
import os

import numpy as np
import pytest

onnx = pytest.importorskip("onnx")
from onnx import TensorProto, helper  # noqa: E402

from server.tts import zerotts_gpu as Z  # noqa: E402


def _tiny_decode(path):
    """Graph tí hon có đủ những chỗ build_graph_model phải sửa: topk là đầu vào, batch động, Cast INT64 → ArgMax."""
    nodes = [
        helper.make_node("Add", ["text_topk", "audio_topk"], ["k"]),
        helper.make_node("Greater", ["x", "u"], ["hit"]),
        helper.make_node("Cast", ["hit"], ["hit_i"], to=TensorProto.INT64),
        helper.make_node("ArgMax", ["hit_i"], ["idx"], axis=1, keepdims=0),
    ]
    g = helper.make_graph(nodes, "t", [
        helper.make_tensor_value_info("x", TensorProto.FLOAT, ["B", 4]),
        helper.make_tensor_value_info("u", TensorProto.FLOAT, ["B", 1]),
        helper.make_tensor_value_info("text_topk", TensorProto.INT64, [1]),
        helper.make_tensor_value_info("audio_topk", TensorProto.INT64, [1]),
    ], [
        helper.make_tensor_value_info("idx", TensorProto.INT64, ["B"]),
        helper.make_tensor_value_info("k", TensorProto.INT64, [1]),
    ])
    onnx.save(helper.make_model(g, opset_imports=[helper.make_opsetid("", 17)]), str(path))


def test_build_graph_model(tmp_path):
    src = tmp_path / "local_frame_decode.onnx"
    _tiny_decode(src)
    dst = Z.build_graph_model(src, tmp_path / "out")
    m = onnx.load(str(dst))
    assert [i.name for i in m.graph.input] == ["x", "u"]
    assert {t.name: list(onnx.numpy_helper.to_array(t)) for t in m.graph.initializer} == \
        {"text_topk": [50], "audio_topk": [25]}
    assert [d.dim_value for d in m.graph.input[0].type.tensor_type.shape.dim] == [1, 4]
    cast = next(n for n in m.graph.node if n.op_type == "Cast")
    assert cast.attribute[0].i == TensorProto.FLOAT

    # ArgMax trên 0/1 dạng float vẫn ra vị trí True đầu tiên như bản INT64
    import onnxruntime as ort
    x = np.array([[0.1, 0.6, 0.2, 0.9]], np.float32)
    u = np.array([[0.5]], np.float32)
    a = ort.InferenceSession(str(src)).run(None, {"x": x, "u": u, "text_topk": np.array([50]),
                                                  "audio_topk": np.array([25])})
    b = ort.InferenceSession(str(dst)).run(None, {"x": x, "u": u})
    assert a[0].tolist() == b[0].tolist() == [1] and b[1].tolist() == [75]

    # Tạo lại chỉ khi file gốc đổi
    meta = json.loads((tmp_path / "out" / "meta.json").read_text(encoding="utf-8"))
    mtime = dst.stat().st_mtime_ns
    Z.build_graph_model(src, tmp_path / "out")
    assert dst.stat().st_mtime_ns == mtime
    _tiny_decode(src)
    os.utime(src, ns=(meta["mtime_ns"] + 10**9, meta["mtime_ns"] + 10**9))
    Z.build_graph_model(src, tmp_path / "out")
    assert json.loads((tmp_path / "out" / "meta.json").read_text(encoding="utf-8"))["mtime_ns"] != meta["mtime_ns"]


def test_build_graph_model_rejects_unknown_graph(tmp_path):
    src = tmp_path / "local_frame_decode.onnx"
    g = helper.make_graph([helper.make_node("Identity", ["x"], ["y"])], "t",
                          [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1])],
                          [helper.make_tensor_value_info("y", TensorProto.FLOAT, [1])])
    onnx.save(helper.make_model(g), str(src))
    with pytest.raises(RuntimeError):
        Z.build_graph_model(src, tmp_path / "out")


@pytest.mark.skipif(os.environ.get("GPU_TESTS") != "1", reason="cần GPU và model ZeroTTS; đặt GPU_TESTS=1")
def test_same_audio_as_zerotts():
    import torch  # noqa: F401  nạp cuDNN trước khi tạo session
    from zerotts import ZeroTTS, hub

    d = hub.resolve_model_dir("zeroweight-ai/ZeroTTS")
    prov = ["CUDAExecutionProvider", "CPUExecutionProvider"]
    base = ZeroTTS(d, providers=prov, warmup=False)
    fast = Z.ZeroTtsGpu(d, providers=prov)
    assert fast.graph is not None, "không bật được CUDA Graph"

    def synth(tts, seed):
        np.random.seed(seed)
        out = [np.asarray(c, np.float32).reshape(-1) for c in tts.synthesize_stream(
            "S3 là dịch vụ object storage, lưu dữ liệu dưới dạng từng object.", voice="maichi",
            first_chunk_frames=2, max_chunk_frames=2)]
        return np.concatenate(out)

    for seed in (1, 2):
        a, b = synth(base, seed), synth(fast, seed)
        assert a.shape == b.shape and np.array_equal(a, b)
    # Tham số khác giá trị đã đóng cứng thì đi đường session gốc, vẫn trùng
    np.random.seed(3)
    a = base.synthesize("Xin chào.", voice="maichi", audio_topk=10)
    np.random.seed(3)
    b = fast.synthesize("Xin chào.", voice="maichi", audio_topk=10)
    assert np.array_equal(np.asarray(a), np.asarray(b))
