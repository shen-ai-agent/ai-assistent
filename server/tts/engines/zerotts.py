"""ZeroTTS: ONNX chạy GPU, stream từng câu, giọng có sẵn.

Chọn theo benchmark Phase 3 (_local/tts_report.md): điểm nghe ngang OmniVoice, TTFA ~80ms, RTF ~0.5, VRAM ~2GB.
"""

from __future__ import annotations

import logging
from typing import Iterator

import numpy as np

from .base import REF_NATIVE_PAUSE_MS, EngineCaps

log = logging.getLogger("tts")


class ZeroTtsEngine:
    REPO = "zeroweight-ai/ZeroTTS"
    caps = EngineCaps(streaming=True, native_pause_ms=REF_NATIVE_PAUSE_MS)

    def __init__(self, voice: str = "maichi", device: str = "cuda", threads: int = 4,
                 first_chunk_frames: int = 1, max_chunk_frames: int = 16, gpu_state: bool = True,
                 cuda_graph: bool = True, temperature: float = 0.8, cfg_scale: float = 1.0):
        providers = ["CPUExecutionProvider"]
        if device == "cuda":
            # onnxruntime-gpu dùng cuDNN/cuBLAS có sẵn trong torch/lib; phải nạp torch trước khi tạo session
            import torch  # noqa: F401
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        from zerotts import ZeroTTS
        if device == "cuda" and gpu_state:
            # Bớt việc cho CPU (CUDA Graph + state nằm trên GPU) để máy bận vẫn đọc kịp, xem zerotts_gpu.py
            from ..zerotts_gpu import ZeroTtsGpu
            self.tts = ZeroTtsGpu.from_pretrained(self.REPO, providers=providers, intra_op_num_threads=threads,
                                                  cuda_graph=cuda_graph)
        else:
            self.tts = ZeroTTS.from_pretrained(self.REPO, providers=providers, intra_op_num_threads=threads)
        self.cuda_graph = getattr(self.tts, "graph", None) is not None
        self.sample_rate = self.tts.sample_rate
        self.voice = voice
        self.chunking = dict(first_chunk_frames=first_chunk_frames, max_chunk_frames=max_chunk_frames)
        # Mỗi câu lấy mẫu độc lập: temperature thấp thì cao độ/độ hơi ít trôi giữa các câu; cfg_scale > 1 kéo về
        # đúng chất giọng mẫu nhưng tốn gấp đôi mỗi frame và không chạy CUDA Graph (bench/tts_consistency.py)
        self.sampling = dict(audio_temperature=temperature, cfg_scale=cfg_scale)
        self.provider = self.tts.prefix_step_sess.get_providers()[0]
        if device == "cuda" and self.provider != "CUDAExecutionProvider":
            # onnxruntime-gpu sai bản CUDA (vd 1.31 cần CUDA 13) thì tự rơi về CPU mà không báo lỗi
            log.warning("ZeroTTS đang chạy %s, không phải GPU: RTF ~1, dễ khựng. Kiểm bản onnxruntime-gpu.",
                        self.provider)

    def describe(self) -> str:
        return f"{self.provider}, CUDA Graph {'bật' if self.cuda_graph else 'tắt'}"

    def voices(self) -> list[str]:
        return self.tts.list_voices()

    def stream(self, text: str, lang: str) -> Iterator[np.ndarray]:
        """Một câu → các mẩu float32 1-D. Mẩu đầu rất ngắn (~80ms audio) để có tiếng sớm."""
        for chunk in self.tts.synthesize_stream(text, voice=self.voice, **self.chunking, **self.sampling):
            yield np.asarray(chunk, dtype=np.float32).reshape(-1)
