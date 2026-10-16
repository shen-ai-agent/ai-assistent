"""Voice server: WebSocket nhận audio push-to-talk, trả transcript; nhận text, stream giọng đọc.
Phục vụ luôn web/ tĩnh.

Mỗi kết nối là một phiên: start → frame PCM → stop → transcript; speak → audio từng câu → speak_end.
STT dùng chung một khoá GPU, TTS một khoá riêng: nhiều tab cùng nói thì xếp hàng, nhưng đang đọc
vẫn nhận dạng được (người dùng nói chen ngang).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Callable

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from . import protocol as P
from .config import ROOT, Config, load_config
from .learn_api import router as learn_router
from .protocol import ErrorCode, State
from .stt.service import SttService, speech_ms
from .tts.synth import TtsService, to_pcm16
from .turnlog import TurnLogger, new_turn_id

log = logging.getLogger("server")
WEB_DIR = ROOT / "web"


def _whisper_engine(cfg: Config):
    from .stt.engine import WhisperEngine  # import muộn: test không cần CUDA
    return WhisperEngine(cfg.model, cfg.compute_type, cfg.device)


def _zerotts_engine(cfg: Config):
    from .tts.synth import ZeroTtsEngine  # import muộn: test không cần model
    return ZeroTtsEngine(cfg.tts_voice, cfg.tts_device, cfg.tts_threads)


def create_app(cfg: Config | None = None, engine_factory: Callable[[Config], object] = _whisper_engine,
               tts_factory: Callable[[Config], object] = _zerotts_engine) -> FastAPI:
    cfg = cfg or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info("Đang load model %s (%s)...", cfg.model, cfg.compute_type)
        engine = await asyncio.to_thread(engine_factory, cfg)
        stt = SttService(engine, language=cfg.language, beam_size=cfg.beam_size,
                         glossary_path=cfg.glossary, corrections_path=cfg.corrections)
        # Warm-up: lượt đầu của CTranslate2 chậm hơn hẳn
        await asyncio.to_thread(stt.transcribe, np.zeros(P.SAMPLE_RATE, dtype=np.float32), stt.topics()[0])
        app.state.cfg = cfg
        app.state.stt = stt
        app.state.learn_checks = {}  # (id đề xuất, chủ đề) → kết quả kiểm tra
        app.state.gpu_lock = asyncio.Lock()
        app.state.turnlog = TurnLogger(cfg.log_dir, cfg.save_audio)
        app.state.tts = None
        app.state.tts_lock = asyncio.Lock()
        if cfg.tts_enabled:
            log.info("Đang load TTS (giọng %s)...", cfg.tts_voice)
            tts = TtsService(await asyncio.to_thread(tts_factory, cfg), loanwords=cfg.tts_loanwords)
            # Warm-up: lần chạy đầu của onnxruntime CUDA chậm (cấp phát, chọn kernel)
            await asyncio.to_thread(lambda: [c for seg in tts.plan("Xin chào bạn.") for c in tts.stream(seg)])
            app.state.tts = tts
        log.info("Sẵn sàng: http://%s:%d", cfg.host, cfg.port)
        yield

    app = FastAPI(title="Voice Study Assistant", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok", "model": cfg.model, "topics": app.state.stt.topics(),
                "tts": app.state.tts is not None}

    app.include_router(learn_router)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        await Session(ws, app, cfg).run()

    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


class Session:
    """Một kết nối WebSocket. Vòng nhận message không bị chặn khi đang nhận dạng, để còn nhận cancel."""

    def __init__(self, ws: WebSocket, app: FastAPI, cfg: Config):
        self.ws, self.cfg = ws, cfg
        self.stt: SttService = app.state.stt
        self.gpu_lock: asyncio.Lock = app.state.gpu_lock
        self.turnlog: TurnLogger = app.state.turnlog
        self.state = State.IDLE
        self.chunks: list[bytes] = []
        self.n_bytes = 0
        self.topic: str | None = None
        self.task: asyncio.Task | None = None
        self.tts: TtsService | None = app.state.tts
        self.tts_lock: asyncio.Lock = app.state.tts_lock
        self.speak_task: asyncio.Task | None = None
        self.speak_id = 0

    async def run(self):
        tts = {"sample_rate": self.tts.sample_rate, "voice": self.cfg.tts_voice} if self.tts else None
        await self.send(P.hello(self.stt.topics(), self.cfg.model, tts))
        await self.set_state(State.IDLE)
        try:
            while True:
                msg = await self.ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    self.on_audio(msg["bytes"])
                elif msg.get("text") is not None:
                    await self.on_text(msg["text"])
        except WebSocketDisconnect:
            pass
        finally:
            if self.task:
                self.task.cancel()
            self.stop_speaking()

    def on_audio(self, data: bytes):
        if self.state is not State.LISTENING:
            return  # frame đến trễ sau stop/cancel
        if self.n_bytes + len(data) > self.cfg.max_audio_s * P.SAMPLE_RATE * 2:
            return
        self.chunks.append(data)
        self.n_bytes += len(data)

    async def on_text(self, text: str):
        try:
            msg = json.loads(text)
            kind = msg["type"]
        except (ValueError, KeyError, TypeError):
            return await self.send(P.error(ErrorCode.BAD_MESSAGE, "Message không phải JSON hợp lệ"))

        if kind == "start":
            topic = msg.get("topic") or None
            if topic is not None and topic not in self.stt.topics():
                return await self.send(P.error(ErrorCode.BAD_TOPIC, f"Không có chủ đề {topic!r}"))
            self.cancel_task()
            self.stop_speaking()  # nói chen ngang thì dừng đọc
            self.chunks, self.n_bytes, self.topic = [], 0, topic
            await self.set_state(State.LISTENING)
        elif kind == "stop":
            if self.state is State.LISTENING:
                await self.finish_turn()
        elif kind == "cancel":
            self.cancel_task()
            self.chunks, self.n_bytes = [], 0
            await self.set_state(State.IDLE)
        elif kind == "correction":
            turn_id, fixed = msg.get("turn_id"), msg.get("text")
            if isinstance(turn_id, str) and isinstance(fixed, str):
                self.turnlog.correction(turn_id, fixed)
        elif kind == "speak":
            text = msg.get("text")
            if not isinstance(text, str) or not text.strip():
                return await self.send(P.error(ErrorCode.BAD_MESSAGE, "speak cần text"))
            if self.tts is None:
                return await self.send(P.error(ErrorCode.TTS_DISABLED, "Server đang tắt TTS"))
            self.stop_speaking()
            self.speak_id += 1
            self.speak_task = asyncio.create_task(self.speak(self.speak_id, text))
        elif kind == "speak_stop":
            self.stop_speaking()
        else:
            await self.send(P.error(ErrorCode.BAD_MESSAGE, f"Không biết message {kind!r}"))

    async def finish_turn(self):
        pcm = b"".join(self.chunks)
        self.chunks, self.n_bytes = [], 0
        audio = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
        audio_ms = round(len(audio) * 1000 / P.SAMPLE_RATE)
        if audio_ms < self.cfg.min_audio_ms:
            await self.send(P.error(ErrorCode.TOO_SHORT, f"Lượt nói quá ngắn ({audio_ms}ms)"))
            return await self.set_state(State.IDLE)
        await self.set_state(State.TRANSCRIBING)
        if self.cfg.min_speech_ms:
            voiced = await asyncio.to_thread(speech_ms, audio)
            if voiced < self.cfg.min_speech_ms:
                self.turnlog.turn(new_turn_id(), audio, skipped="no_speech", audio_ms=audio_ms, speech_ms=voiced)
                await self.send(P.error(ErrorCode.NO_SPEECH, f"Không nghe thấy tiếng nói ({voiced}ms)"))
                return await self.set_state(State.IDLE)
        self.task = asyncio.create_task(self.transcribe(audio, audio_ms, self.topic))

    async def transcribe(self, audio: np.ndarray, audio_ms: int, topic: str | None):
        turn_id = new_turn_id()
        try:
            async with self.gpu_lock:
                job = asyncio.ensure_future(asyncio.to_thread(self.stt.transcribe, audio, topic))
                try:
                    res = await asyncio.shield(job)
                except asyncio.CancelledError:
                    # Luồng GPU không dừng được giữa chừng: giữ khoá tới khi xong để lượt sau
                    # không chạy chồng, rồi bỏ kết quả
                    await asyncio.wait([job])
                    return
        except Exception as e:
            log.exception("STT lỗi")
            await self.send(P.error(ErrorCode.STT_FAILED, str(e)))
            await self.set_state(State.IDLE)
            return
        self.turnlog.turn(turn_id, audio, topic=res.topic, topic_auto=res.topic_auto,
                          audio_ms=audio_ms, stt_ms=res.stt_ms, raw=res.raw, text=res.text)
        await self.send(P.transcript(turn_id, res.text, res.topic, res.topic_auto, audio_ms, res.stt_ms))
        await self.set_state(State.IDLE)

    async def speak(self, speak_id: int, text: str):
        """Đọc từng câu, gửi từng mẩu audio ngay khi có. Bị huỷ (speak_stop, speak mới) thì speak_end cancelled."""
        t0 = time.perf_counter()
        ttfa_ms, n_samples, cancelled = None, 0, False
        try:
            segs = await asyncio.to_thread(self.tts.plan, text)
            await self.send(P.speak_start(speak_id, [{"text": s.text, "source": s.source, "lang": s.lang}
                                                     for s in segs]))
            async with self.tts_lock:
                for i, seg in enumerate(segs):
                    await self.send(P.speak_sentence(speak_id, i))
                    it = self.tts.stream(seg)
                    try:
                        while True:
                            job = asyncio.ensure_future(asyncio.to_thread(next, it, None))
                            try:
                                chunk = await asyncio.shield(job)
                            except asyncio.CancelledError:
                                # Mẩu đang tổng hợp không dừng giữa chừng được: chờ xong rồi mới nhả khoá
                                await asyncio.wait([job])
                                raise
                            if chunk is None:
                                break
                            if ttfa_ms is None:
                                ttfa_ms = round((time.perf_counter() - t0) * 1000)
                            n_samples += len(chunk)
                            await self.send_bytes(P.audio_frame(speak_id, to_pcm16(chunk)))
                    finally:
                        it.close()
        except asyncio.CancelledError:
            cancelled = True
        except Exception as e:
            log.exception("TTS lỗi")
            await self.send(P.error(ErrorCode.TTS_FAILED, str(e)))
            cancelled = True
        await self.send(P.speak_end(speak_id, cancelled, ttfa_ms, round((time.perf_counter() - t0) * 1000),
                                    round(n_samples * 1000 / self.tts.sample_rate)))

    def stop_speaking(self):
        if self.speak_task and not self.speak_task.done():
            self.speak_task.cancel()
        self.speak_task = None

    def cancel_task(self):
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = None

    async def set_state(self, value: State):
        self.state = value
        await self.send(P.state(value))

    async def send(self, msg: dict):
        try:
            await self.ws.send_json(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass  # client đã đóng

    async def send_bytes(self, data: bytes):
        try:
            await self.ws.send_bytes(data)
        except (WebSocketDisconnect, RuntimeError):
            pass
