"""Voice server: WebSocket nhận audio push-to-talk, trả transcript; hỏi Claude qua bridge; stream giọng đọc.
Phục vụ luôn web/ tĩnh.

Mỗi kết nối là một phiên: start → frame PCM → stop → transcript → (bridge) answer_* → audio từng câu → speak_end.
Bật rảnh tay (listen) thì frame PCM đi liên tục, server tự cắt đoạn nói và chờ gọi tên (handsfree.py).
STT dùng chung một khoá GPU, TTS một khoá riêng: nhiều tab cùng nói thì xếp hàng, nhưng đang đọc
vẫn nhận dạng được (người dùng nói chen ngang).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import aclosing, asynccontextmanager
from typing import AsyncIterator, Callable

import numpy as np
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import protocol as P
from .bridge import Bridge, BridgeError, Done, SentenceFeeder, TextDelta, make_bridge
from .bridge.asklog import AskLogger
from .config import ROOT, Config, load_config
from .assistant.commands import Command, match_command
from .agent.tasks import Pending, TaskManager, ack_text
from .handsfree import HandsFree
from .learn_api import router as learn_router
from .protocol import ErrorCode, ListenMode, State
from .stt.service import SttService, speech_ms
from .study import Library, StudyError, StudyService
from .study.api import router as study_router
from .tts.synth import TtsService, to_pcm16
from .turnlog import TurnLogger, new_turn_id

log = logging.getLogger("server")
WEB_DIR = ROOT / "web"


def _whisper_engine(cfg: Config):
    from .stt.engine import WhisperEngine  # import muộn: test không cần CUDA
    return WhisperEngine(cfg.model, cfg.compute_type, cfg.device)


def _zerotts_engine(cfg: Config):
    from .tts.synth import ZeroTtsEngine  # import muộn: test không cần model
    return ZeroTtsEngine(cfg.tts_voice, cfg.tts_device, cfg.tts_threads,
                         first_chunk_frames=cfg.tts_first_chunk_frames, max_chunk_frames=cfg.tts_max_chunk_frames)


def _silero_session():
    from faster_whisper.vad import get_vad_model
    return get_vad_model().session


def _vscode_agent(cfg: Config):
    """Danh bạ dự án + bộ mở VS Code cho tính năng giao việc (T-011). None = tắt."""
    if not cfg.agent_enabled:
        return None
    from .agent.projects import ProjectRegistry
    from .agent.vscode import VsCodeLauncher
    registry = ProjectRegistry(cfg.agent_roots, cfg.agent_projects_file, cfg.agent_claude_dir)
    launcher = VsCodeLauncher(cfg.agent_code_exe or None, auto_submit=cfg.agent_auto_submit,
                              window_wait_s=cfg.agent_window_wait_s)
    return registry, launcher


def _claude_tutor(cfg: Config):
    """Gia sư cho tab Học (T-012): claude -p chạy trong thư mục tài liệu, prompt giảng bài.
    None = tắt tab Học (bridge không phải claude_cli)."""
    if cfg.bridge_kind != "claude_cli":
        return None
    from .bridge.claude_cli import ClaudeCliBridge
    return lambda workdir: ClaudeCliBridge(workdir, cfg.study_prompt, cfg.study_tools, cfg.bridge_model,
                                           cfg.study_timeout_s)


async def _preset_answer(text: str) -> AsyncIterator[TextDelta | Done]:
    yield TextDelta(text)
    yield Done()


def create_app(cfg: Config | None = None, engine_factory: Callable[[Config], object] = _whisper_engine,
               tts_factory: Callable[[Config], object] = _zerotts_engine,
               bridge_factory: Callable[[Config], Bridge | None] = make_bridge,
               vad_factory: Callable[[], object] = _silero_session,
               agent_factory: Callable[[Config], tuple | None] = _vscode_agent,
               tutor_factory: Callable[[Config], Callable | None] = _claude_tutor) -> FastAPI:
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
        app.state.sessions = []  # kết nối đang mở, mới nhất ở cuối
        app.state.vad_session = await asyncio.to_thread(vad_factory)  # ONNX CPU, các phiên dùng chung
        app.state.asklog = AskLogger(cfg.bridge_log_dir)
        app.state.bridge = bridge = bridge_factory(cfg)
        if bridge is not None:
            log.info("Bridge: %s", bridge.name)
            if hasattr(bridge, "start"):
                bridge.start()
                # Hotkey khi không có lượt nào chờ (câu hỏi gõ thẳng trong Claude): đọc ở tab mới nhất
                bridge.on_orphan = lambda text: app.state.sessions and app.state.sessions[-1].start_ask(
                    "(clipboard)", _preset_answer(text))
        make_tutor = tutor_factory(cfg)
        app.state.study = StudyService(Library(cfg.study_dir, cfg.study_max_chars), make_tutor) if make_tutor else None
        app.state.agent = None
        if parts := agent_factory(cfg):
            registry, launcher = parts

            async def broadcast(msg: dict):
                for s in list(app.state.sessions):
                    await s.send(msg)

            async def notify(task, text: str):
                sessions = app.state.sessions
                target = task.origin if task.origin in sessions else sessions[-1] if sessions else None
                if target is not None:
                    await target.notify_speak(text)

            app.state.agent = TaskManager(registry, launcher, broadcast=broadcast, notify=notify,
                                          claude_dir=cfg.agent_claude_dir, log_dir=cfg.agent_log_dir,
                                          stall_s=cfg.agent_stall_s, submit_timeout_s=cfg.agent_submit_timeout_s,
                                          task_timeout_s=cfg.agent_task_timeout_s,
                                          on_label=app.state.turnlog.intent)
            log.info("Giao việc VS Code: %d dự án", len(registry.all()))
        log.info("Sẵn sàng: http://%s:%d", cfg.host, cfg.port)
        yield
        if app.state.agent is not None:
            app.state.agent.close()
        if bridge is not None and hasattr(bridge, "stop"):
            bridge.stop()

    app = FastAPI(title="Voice Study Assistant", lifespan=lifespan)

    @app.get("/health")
    async def health():
        bridge = app.state.bridge
        return {"status": "ok", "model": cfg.model, "topics": app.state.stt.topics(),
                "tts": app.state.tts is not None, "bridge": bridge.name if bridge else None}

    @app.get("/lan.crt")
    async def lan_cert():
        """Chứng chỉ tự ký của cổng LAN (không kèm khoá), để điện thoại tải về cài làm CA.
        Có tin chứng chỉ thì Chrome mới chạy service worker và cho cài PWA."""
        if not cfg.lan_cert.exists():
            raise HTTPException(404, "Chưa tạo chứng chỉ LAN")
        return FileResponse(cfg.lan_cert, media_type="application/x-x509-ca-cert", filename="mimi-lan.crt")

    app.include_router(learn_router)
    app.include_router(study_router)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        await Session(ws, app, cfg).run()

    if WEB_DIR.exists():
        app.mount("/", NoCacheStatic(directory=WEB_DIR, html=True), name="web")
    return app


class NoCacheStatic(StaticFiles):
    """Web không có bước build nên tên file không đổi theo phiên bản: bắt trình duyệt hỏi lại server
    (ETag, file không đổi thì 304), để sửa CSS/JS xong tải lại trang là thấy ngay, kể cả trên điện thoại."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


class Session:
    """Một kết nối WebSocket. Vòng nhận message không bị chặn khi đang nhận dạng, để còn nhận cancel."""

    def __init__(self, ws: WebSocket, app: FastAPI, cfg: Config):
        self.ws, self.cfg, self.app = ws, cfg, app
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
        self.bridge: Bridge | None = app.state.bridge
        self.asklog: AskLogger = app.state.asklog
        self.ask_task: asyncio.Task | None = None
        self.ask_id = 0
        self.last_answer = ""   # câu trả lời xong gần nhất, cho lệnh "đọc lại"
        self.hf: HandsFree | None = None
        self.agent: TaskManager | None = app.state.agent
        self.work = False                  # chế độ công việc: tab Công việc đang mở (message work)
        self.focus: str | None = None      # dự án đang chọn ở tab Công việc
        self.pending: Pending | None = None   # Mimi vừa hỏi "dự án nào?", chờ câu trả lời
        self.last_heard: tuple[str, str] | None = None   # (turn_id, text) của lượt nhận dạng gần nhất
        self.study: StudyService | None = app.state.study
        self.study_doc: str | None = None  # tài liệu đang mở ở tab Học (message study): câu hỏi đi tới gia sư
        self.generate_tasks: set[asyncio.Task] = set()  # lượt generate đang chạy, huỷ khi đóng kết nối

    async def run(self):
        tts = {"sample_rate": self.tts.sample_rate, "voice": self.cfg.tts_voice} if self.tts else None
        bridge = self.bridge and {"kind": self.bridge.name, "auto_ask": self.cfg.bridge_auto_ask,
                                  "session_id": self.bridge.session_id, "model": getattr(self.bridge, "model", None),
                                  "models": self.cfg.bridge_models if hasattr(self.bridge, "model") else []}
        assistant = {"name": self.cfg.assistant_name, "sleep_silence_ms": self.cfg.assistant_sleep_silence_ms,
                     "active_silence_ms": self.cfg.assistant_active_silence_ms}
        agent = self.agent and {"auto_submit": self.cfg.agent_auto_submit}
        study = self.study and {"max_chars": self.cfg.study_max_chars}
        await self.send(P.hello(self.stt.topics(), self.cfg.model, tts, bridge, assistant, agent, study))
        await self.set_state(State.IDLE)
        if self.agent:
            await self.send(P.tasks(self.agent.snapshot()))
        self.app.state.sessions.append(self)
        try:
            while True:
                msg = await self.ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    await self.on_audio(msg["bytes"])
                elif msg.get("text") is not None:
                    await self.on_text(msg["text"])
        except WebSocketDisconnect:
            pass
        finally:
            self.app.state.sessions.remove(self)
            if self.task:
                self.task.cancel()
            if self.hf:
                self.hf.close()
            self.stop_asking()
            self.stop_speaking()
            for t in self.generate_tasks:
                t.cancel()  # bridge kill tiến trình claude

    async def on_audio(self, data: bytes):
        if self.state is not State.LISTENING:
            if self.hf:
                await self.hf.push(data)
            return  # không rảnh tay: frame đến trễ sau stop/cancel
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
            self.stop_asking()  # nói chen ngang thì dừng hỏi và dừng đọc
            self.stop_speaking()
            if self.hf:
                await self.hf.reset()  # giữ Space thắng rảnh tay
            self.chunks, self.n_bytes, self.topic = [], 0, topic
            await self.set_state(State.LISTENING)
        elif kind == "stop":
            if self.state is State.LISTENING:
                await self.finish_turn()
        elif kind == "cancel":
            self.cancel_task()
            self.chunks, self.n_bytes = [], 0
            if self.hf:
                await self.hf.reset()
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
            queue = self.begin_speaking()[1]
            queue.put_nowait(text)
            queue.put_nowait(None)
        elif kind == "speak_stop":
            self.stop_speaking()
            if self.hf:
                await self.hf.reset()
        elif kind == "ask":
            text = msg.get("text")
            if not isinstance(text, str) or not text.strip():
                return await self.send(P.error(ErrorCode.BAD_MESSAGE, "ask cần text"))
            if self.bridge is None:
                return await self.send(P.error(ErrorCode.BRIDGE_DISABLED, "Server chưa bật bridge tới Claude"))
            self.start_ask(text, source="text")
        elif kind == "ask_stop":
            self.stop_asking()
            self.stop_speaking()
            if self.hf:
                await self.hf.reset()
        elif kind == "new_session":
            if self.bridge is not None:
                self.stop_asking()
                self.bridge.new_session()
                await self.send(P.bridge_session(self.bridge.session_id))
        elif kind == "set_model":
            model = msg.get("model")
            if self.bridge is None or not hasattr(self.bridge, "model"):
                return await self.send(P.error(ErrorCode.BRIDGE_DISABLED, "Bridge này không chọn được model"))
            if model != "" and model not in self.cfg.bridge_models:
                return await self.send(P.error(ErrorCode.BAD_MESSAGE, f"Model {model!r} không có trong [bridge] models"))
            self.bridge.model = model  # bridge dùng chung nên đổi cho mọi tab, lượt đang chạy giữ model cũ
            for s in list(self.app.state.sessions):
                await s.send(P.bridge_model(model))
        elif kind == "listen":
            topic = msg.get("topic") or None
            if topic is not None and topic not in self.stt.topics():
                return await self.send(P.error(ErrorCode.BAD_TOPIC, f"Không có chủ đề {topic!r}"))
            if not msg.get("on"):
                if self.hf:
                    self.hf.close()
                    self.hf = None
                await self.send(P.listen(ListenMode.OFF))
            elif self.hf:
                self.hf.topic = topic  # đang bật: chỉ đổi chủ đề
            else:
                self.hf = HandsFree(self, self.app.state.vad_session, topic)
                await self.send(P.listen(self.hf.mode))
        elif kind == "generate":
            await self.on_generate(msg)
        elif kind == "study":
            await self.on_study(msg.get("doc_id"))
        elif kind in ("projects", "task", "task_cancel", "task_open", "task_fix", "work"):
            await self.on_task_message(kind, msg)
        else:
            await self.send(P.error(ErrorCode.BAD_MESSAGE, f"Không biết message {kind!r}"))

    async def on_generate(self, msg: dict):
        """Message generate: lượt claude -p độc lập, chạy nền để vòng nhận message không bị chặn."""
        req_id, system, text, model = (msg.get(k) for k in ("req_id", "system", "text", "model"))
        if not isinstance(req_id, str) or not req_id:
            return await self.send(P.error(ErrorCode.BAD_MESSAGE, "generate cần req_id"))
        if not isinstance(system, str) or not isinstance(text, str) or not text.strip() or not isinstance(model, str):
            return await self.send(P.generate_result(req_id, None, "generate cần system, text và model", 0))
        if self.bridge is None or not hasattr(self.bridge, "generate"):
            return await self.send(P.generate_result(req_id, None, 'generate cần [bridge] kind = "claude_cli"', 0))
        if model not in self.cfg.bridge_models:
            return await self.send(P.generate_result(req_id, None, f"Model {model!r} không có trong [bridge] models", 0))

        async def run():
            t0 = time.perf_counter()
            out, err = None, None
            try:
                out = await self.bridge.generate(system, text, model)
            except (BridgeError, OSError) as e:  # OSError: không chạy được tiến trình claude
                err = str(e)
            ms = round((time.perf_counter() - t0) * 1000)
            log.info("generate %s (%s): %d ms%s", req_id, model, ms, f", lỗi: {err}" if err else "")
            await self.send(P.generate_result(req_id, out, err, ms))

        task = asyncio.create_task(run())
        self.generate_tasks.add(task)
        task.add_done_callback(self.generate_tasks.discard)

    async def on_task_message(self, kind: str, msg: dict):
        if self.agent is None:
            return await self.send(P.error(ErrorCode.AGENT_DISABLED, "Server đang tắt tính năng giao việc VS Code"))
        if kind == "projects":
            await self.send(P.projects(self.agent.projects(refresh=True)))
        elif kind == "task":
            text, project = msg.get("text", ""), msg.get("project") or None
            if not isinstance(text, str) or not (text.strip() or project):
                return await self.send(P.error(ErrorCode.BAD_MESSAGE, "task cần text hoặc project"))
            self.agent.start_text(text.strip(), project if isinstance(project, str) else None, origin=self)
        elif kind == "task_cancel":
            self.agent.cancel(msg.get("task_id"))
        elif kind == "task_open":
            if not await self.agent.open_session(msg.get("task_id")):
                await self.send(P.error(ErrorCode.BAD_MESSAGE, "Không mở lại được phiên của task này"))
        elif kind == "task_fix":
            project, request = msg.get("project"), msg.get("request")
            if not isinstance(project, str) or (request is not None and not isinstance(request, str)):
                return await self.send(P.error(ErrorCode.BAD_MESSAGE, "task_fix cần project"))
            if self.agent.fix(msg.get("task_id"), project, request, origin=self) is None:
                await self.send(P.error(ErrorCode.BAD_MESSAGE, f"Không giao lại được task sang dự án {project!r}"))
        elif kind == "work":
            project = msg.get("project")
            self.work = bool(msg.get("on"))
            self.focus = project if isinstance(project, str) and project else None

    async def on_study(self, doc_id):
        """Tab Học mở / đóng một tài liệu."""
        if doc_id is None:
            self.study_doc = None
            return
        if self.study is None:
            return await self.send(P.error(ErrorCode.STUDY_DISABLED, 'Tab Học cần [bridge] kind = "claude_cli"'))
        try:
            doc = self.study.library.get(doc_id)
        except StudyError as e:
            self.study_doc = None
            return await self.send(P.error(ErrorCode.BAD_MESSAGE, str(e)))
        self.study_doc = doc.id
        await self.send(P.study_doc(doc.public()))

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

    async def run_gpu(self, fn, *args):
        """Chạy fn trong thread, xếp hàng theo khoá GPU chung của STT."""
        async with self.gpu_lock:
            job = asyncio.ensure_future(asyncio.to_thread(fn, *args))
            try:
                return await asyncio.shield(job)
            except asyncio.CancelledError:
                # Luồng GPU không dừng được giữa chừng: giữ khoá tới khi xong để lượt sau
                # không chạy chồng, rồi bỏ kết quả
                await asyncio.wait([job])
                raise

    async def recognize(self, audio: np.ndarray, audio_ms: int, topic: str | None, **log_fields):
        """Nhận dạng một lượt, ghi log, gửi transcript. Lỗi thì báo client và trả None."""
        turn_id = new_turn_id()
        names = self.agent.stt_names() if self.agent and self.work else None  # tab Công việc: nghe đúng tên dự án
        if names:
            log_fields["names"] = len(names)
        try:
            res = await self.run_gpu(self.stt.transcribe, audio, topic, None, names)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("STT lỗi")
            await self.send(P.error(ErrorCode.STT_FAILED, str(e)))
            return None
        self.turnlog.turn(turn_id, audio, topic=res.topic, topic_auto=res.topic_auto,
                          audio_ms=audio_ms, stt_ms=res.stt_ms, raw=res.raw, text=res.text, **log_fields)
        self.last_heard = (turn_id, res.text)
        await self.send(P.transcript(turn_id, res.text, res.topic, res.topic_auto, audio_ms, res.stt_ms))
        return res

    async def transcribe(self, audio: np.ndarray, audio_ms: int, topic: str | None):
        try:
            res = await self.recognize(audio, audio_ms, topic)
        except asyncio.CancelledError:
            return
        if res is None:
            return await self.set_state(State.IDLE)
        if self.can_ask() and (self.cfg.bridge_auto_ask or self.work or self.study_doc) and res.text.strip():
            self.start_ask(res.text)
        else:
            await self.set_state(State.IDLE)

    def start_ask(self, question: str, stream: AsyncIterator[TextDelta | Done] | None = None, on_spoken=None,
                  source: str = "voice"):
        """Hỏi Claude (thay lượt đang chạy). stream có sẵn = câu trả lời đã có (hotkey clipboard).
        on_spoken: gọi khi đọc xong câu trả lời (xem begin_speaking).
        Câu ngắn là lệnh ("mở session mới", "dừng", "đọc lại") thì chạy ngay, không hỏi Claude.
        Câu giao việc ("mở dự án X rồi ...") thì giao cho Claude trong VS Code (T-011).
        Tab Học đang mở tài liệu: câu hỏi đi tới gia sư của tài liệu đó (T-012)."""
        self.stop_asking()
        cmd = match_command(question) if stream is None else None
        if cmd is not None:
            self.ask_task = asyncio.create_task(self.run_command(cmd, question, on_spoken))
            return
        if self.study_doc is not None and self.study is not None and stream is None:
            doc_id = self.study_doc
            self.ask_id += 1

            async def on_doc(doc):
                await self.send(P.study_doc(doc.public()))

            events = self.study.ask(doc_id, question, getattr(self.bridge, "model", None), on_doc)
            self.ask_task = asyncio.create_task(self.answer(self.ask_id, question, events, on_spoken, doc_id))
            return
        route = self.route_task(question, source) if self.agent and stream is None else None
        if route is not None and route.dev is not None:
            task = self.agent.start(route.dev, source, origin=self, run=False, turn_id=route.turn_id)
            self.ask_task = asyncio.create_task(self.reply_command(
                "dev_task", question, ack_text(task), on_spoken, then=lambda: self.agent.launch(task)))
            return
        if route is not None:
            self.ask_task = asyncio.create_task(self.reply_command("work", question, route.reply, on_spoken,
                                                                   choices=route.choices))
            return
        if self.bridge is None:
            return
        self.ask_id += 1
        self.ask_task = asyncio.create_task(
            self.answer(self.ask_id, question, stream or self.bridge.ask(question), on_spoken))

    def can_ask(self) -> bool:
        """Có ai nhận câu hỏi không: Claude (bridge), chế độ công việc giao cho VS Code, hoặc gia sư tab Học."""
        return (self.bridge is not None or (self.agent is not None and self.work)
                or (self.study is not None and self.study_doc is not None))

    def route_task(self, question: str, source: str = "voice"):
        """Câu này là việc cho VS Code / câu hỏi tiến độ (Route) hay để hỏi Claude (None)."""
        pending = self.pending if self.pending and not self.pending.expired else None
        # turn_id chỉ khi câu đến từ lượt nhận dạng vừa rồi (rảnh tay đã bỏ tên trợ lý nên chỉ là một phần)
        heard = self.last_heard if source != "text" else None
        turn_id = heard[0] if heard and question.strip() and question.strip() in heard[1] else None
        route = self.agent.route(question, work=self.work, focus=self.focus, pending=pending, turn_id=turn_id)
        self.pending = route.pending if route else None
        return route

    async def run_command(self, cmd: Command, text: str, on_spoken=None):
        reply = None
        if cmd is Command.STOP:
            self.stop_speaking()
        elif cmd is Command.NEW_SESSION and self.study_doc is not None:
            reply = "Ở tab Học, muốn học lại từ đầu thì bấm nút Học lại."
        elif cmd is Command.NEW_SESSION:
            if self.bridge is not None:
                self.bridge.new_session()
                await self.send(P.bridge_session(self.bridge.session_id))
            self.last_answer = ""
            reply = "Đã mở cuộc trò chuyện mới."
        elif cmd is Command.REPEAT:
            reply = self.last_answer or "Chưa có câu trả lời nào để đọc lại."
        await self.reply_command(cmd, text, reply, on_spoken)

    async def reply_command(self, name: str, text: str, reply: str | None, on_spoken=None, then=None,
                            choices: list[str] | None = None):
        """Báo web một lệnh đã chạy và đọc câu đáp (nếu có). then(): chạy ngay sau khi web nhận lệnh.
        choices: tên dự án Mimi vừa hỏi lại, web hiện thành nút chọn."""
        try:
            await self.send(P.command(name, text, reply, choices))
        finally:
            if then:
                then()  # kể cả khi bị huỷ (nói chen ngang): việc đã nhận thì vẫn làm
        await self.set_state(State.IDLE)
        if reply and self.tts:
            queue = self.begin_speaking(on_spoken)[1]
            queue.put_nowait(reply)
            queue.put_nowait(None)
        elif on_spoken:
            await on_spoken(name == Command.STOP, None, 0, 0)  # dừng: rảnh tay về SLEEP

    async def notify_speak(self, text: str):
        """Đọc thông báo của task nền (xong việc, chờ duyệt) nếu tab đang rảnh; đang bận thì chỉ hiện trên web."""
        busy = (self.state is not State.IDLE or (self.speak_task and not self.speak_task.done())
                or (self.ask_task and not self.ask_task.done())
                or (self.hf is not None and self.hf.mode is not ListenMode.SLEEP))
        if self.tts is None or busy:
            return
        queue = self.begin_speaking()[1]
        queue.put_nowait(text)
        queue.put_nowait(None)

    async def answer(self, ask_id: int, question: str, stream: AsyncIterator[TextDelta | Done], on_spoken=None,
                     doc_id: str | None = None):
        """Chuyển text Claude về web và cắt dần thành câu cho TTS, để câu đầu được đọc khi Claude còn đang viết.
        doc_id: lượt giảng của tài liệu ở tab Học (stream là StudyService.ask)."""
        t0 = time.perf_counter()
        speak_id, queue = self.begin_speaking(on_spoken) if self.tts else (None, None)
        await self.send(P.answer_start(ask_id, question, speak_id, doc_id))
        await self.set_state(State.WAITING_CLAUDE)
        feeder = SentenceFeeder()
        parts: list[str] = []
        first_ms = error = done = None
        cancelled = False
        try:
            async with aclosing(stream) as events:  # đóng generator ngay = bridge kill tiến trình claude
                async for ev in events:
                    if isinstance(ev, Done):
                        done = ev
                        continue
                    if first_ms is None:
                        first_ms = round((time.perf_counter() - t0) * 1000)
                    parts.append(ev.text)
                    await self.send(P.answer_delta(ask_id, ev.text))
                    if queue:
                        for chunk in feeder.push(ev.text):
                            queue.put_nowait(chunk)
        except asyncio.CancelledError:
            cancelled = True
        except Exception as e:
            if not isinstance(e, BridgeError):
                log.exception("Bridge lỗi")
            error = str(e) or type(e).__name__
            await self.send(P.error(ErrorCode.BRIDGE_FAILED, error))
        if queue:
            if not cancelled:
                for chunk in feeder.flush():
                    queue.put_nowait(chunk)
                if error:
                    queue.put_nowait("Không hỏi được Claude, xem lỗi trên màn hình.")
            queue.put_nowait(None)
        total_ms = round((time.perf_counter() - t0) * 1000)
        session_id = done.session_id if done else None if doc_id else self.bridge and self.bridge.session_id
        cost = done.cost_usd if done else None
        model = done.model if done else None
        await self.send(P.answer_end(ask_id, cancelled, error, session_id, first_ms, total_ms, cost, model))
        if not cancelled and not error:
            self.last_answer = "".join(parts)
        self.asklog.write(bridge=self.bridge and self.bridge.name, session_id=session_id, question=question,
                          answer="".join(parts), first_text_ms=first_ms, total_ms=total_ms, cost_usd=cost,
                          model=model, error=error, cancelled=cancelled, **({"study": doc_id} if doc_id else {}))
        if self.state is State.WAITING_CLAUDE:
            await self.set_state(State.IDLE)
        if on_spoken and not self.tts:
            await on_spoken(cancelled, None, 0, 0)

    def begin_speaking(self, on_end=None) -> tuple[int, asyncio.Queue]:
        """Mở lượt đọc mới (thay lượt cũ). Đưa text vào queue dần, None = hết.
        on_end: async (cancelled, ttfa_ms, synth_ms, audio_ms), gọi sau speak_end."""
        self.stop_speaking()
        self.speak_id += 1
        queue: asyncio.Queue = asyncio.Queue()
        self.speak_task = asyncio.create_task(self.speak(self.speak_id, queue, on_end))
        return self.speak_id, queue

    async def speak(self, speak_id: int, queue: asyncio.Queue, on_end=None):
        """Đọc từng câu, gửi từng mẩu audio ngay khi có. Bị huỷ (speak_stop, speak mới) thì speak_end cancelled."""
        t0 = time.perf_counter()
        ttfa_ms, n_samples, cancelled, index = None, 0, False, 0
        await self.send(P.speak_start(speak_id))
        try:
            while (text := await queue.get()) is not None:
                segs = await asyncio.to_thread(self.tts.plan, text)
                if not segs:
                    continue
                await self.send(P.speak_more(speak_id, [{"text": s.text, "source": s.source, "lang": s.lang}
                                                        for s in segs]))
                for seg in segs:
                    async with self.tts_lock:  # khoá theo câu: tab khác chen được giữa hai câu
                        await self.send(P.speak_sentence(speak_id, index))
                        index += 1
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
        synth_ms, audio_ms = round((time.perf_counter() - t0) * 1000), round(n_samples * 1000 / self.tts.sample_rate)
        await self.send(P.speak_end(speak_id, cancelled, ttfa_ms, synth_ms, audio_ms))
        if on_end:
            await on_end(cancelled, ttfa_ms, synth_ms, audio_ms)

    def stop_asking(self):
        if self.ask_task and not self.ask_task.done():
            self.ask_task.cancel()
        self.ask_task = None

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
