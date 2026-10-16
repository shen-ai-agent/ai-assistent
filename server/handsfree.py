"""Chế độ rảnh tay của một phiên (T-010): mic luôn mở, gọi tên mới nghe câu hỏi.

SLEEP   mỗi đoạn nói (VAD cắt, im lặng sleep_silence_ms) được nhận dạng không prompt, tìm tên ở đầu câu.
        Có tên, sau tên còn ≥ min_rest_words từ ("Mimi ơi, EC2 là gì") → hỏi luôn.
        Chỉ có tên → đọc một câu đáp ngẫu nhiên, đọc xong thì ACTIVE.
ACTIVE  đoạn nói kế tiếp là câu hỏi, nhận dạng với glossary như push-to-talk. Hết hạn mà không nói → SLEEP.
BUSY    đang nhận dạng / nghĩ / đọc. Đoạn nói chỉ dùng để tìm tên (ngắt lời), không coi là câu hỏi,
        vì mic có thể thu lại tiếng loa. Đọc xong → ACTIVE thêm follow_up_s để hỏi tiếp không cần gọi tên.

Mốc thời gian tính bằng số mẫu mic đã nhận (đồng hồ của Segmenter), nên test được chỉ bằng cách đẩy PCM.
Server không biết chính xác khi nào loa phát xong: ước lượng từ lúc bắt đầu đọc + độ dài audio.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING

import numpy as np

from . import protocol as P
from .assistant import Segment, Segmenter, StreamingVad, WakeMatcher
from .assistant.vad import WINDOW
from .assistant.wake import decide
from .protocol import ListenMode, State

if TYPE_CHECKING:
    from .app import Session

log = logging.getLogger("server.handsfree")
SR = P.SAMPLE_RATE
PLAYBACK_MARGIN_MS = 400   # mạng + bộ đệm phát của trình duyệt


class HandsFree:
    def __init__(self, session: "Session", vad_session, topic: str | None = None):
        self.s, cfg = session, session.cfg
        self.cfg = cfg
        self.topic = topic
        self.matcher = WakeMatcher(cfg.assistant_name, cfg.assistant_aliases, min_score=cfg.assistant_wake_min_score)
        self.seg = Segmenter(StreamingVad(vad_session), silence_ms=cfg.assistant_sleep_silence_ms,
                             max_s=cfg.assistant_max_segment_s)
        self.mode = ListenMode.SLEEP
        self.window = (0, 0)                       # ACTIVE: đoạn nói bắt đầu trong khoảng này là câu hỏi
        self.pending: tuple[int, int] | None = None  # cửa sổ ACTIVE sẽ mở khi loa phát xong
        self.epoch = 0                             # tăng khi reset: bỏ kết quả của đoạn nói cũ
        self.handling = False
        self.last_ack: str | None = None
        self.speak_seq = 0                         # lượt đọc mới nhất mà rảnh tay đang chờ
        self.ack_from: int | None = None           # đoạn nói bắt đầu trước mốc này là nói tiếp sau tên, không phải tiếng loa
        self.last_start = -1                       # mốc bắt đầu của đoạn nói gần nhất
        self.queue: asyncio.Queue[tuple[int, Segment, bool]] = asyncio.Queue()
        self.worker = asyncio.create_task(self._run())

    def close(self):
        self.worker.cancel()

    async def reset(self, mode: ListenMode = ListenMode.SLEEP):
        """Bỏ mọi đoạn đang chờ (người dùng bấm dừng, giữ Space để nói)."""
        self.epoch += 1
        self.pending = self.ack_from = None
        self.last_start = -1
        self.seg.reset()
        await self.set_mode(mode)

    async def set_mode(self, mode: ListenMode):
        self.mode = mode
        self.seg.silence_ms = (self.cfg.assistant_active_silence_ms if mode is ListenMode.ACTIVE
                               else self.cfg.assistant_sleep_silence_ms)
        await self.s.send(P.listen(mode))

    async def push(self, pcm: bytes):
        audio = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
        for kind, value in self.seg.push(audio):
            await self.s.send(P.vad(kind == "start"))
            if kind == "start":
                self.last_start = value
            if kind == "end":
                lo, hi = self.window
                question = self.mode is ListenMode.ACTIVE and lo - self.seg.preroll * WINDOW <= value.start <= hi
                if question:
                    await self.set_mode(ListenMode.BUSY)  # đoạn sau (kể cả tiếng loa) chỉ còn tìm tên
                self.queue.put_nowait((self.epoch, value, question))
        await self._tick()

    async def _tick(self):
        pos = self.seg.pos
        if self.mode is ListenMode.BUSY and self.pending and pos >= self.pending[0]:
            self.window, self.pending = self.pending, None
            await self.set_mode(ListenMode.ACTIVE if self.window[1] > self.window[0] else ListenMode.SLEEP)
        elif (self.mode is ListenMode.ACTIVE and pos > self.window[1] and not self.seg.in_speech
              and not self.handling and self.queue.empty()):
            await self.set_mode(ListenMode.SLEEP)

    async def _run(self):
        while True:
            epoch, seg, question = await self.queue.get()
            if epoch != self.epoch:
                continue
            self.handling = True
            try:
                if question:
                    await self._question(seg, epoch)
                else:
                    await self._wake_check(seg, epoch)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("Rảnh tay lỗi")
                await self.s.send(P.error(P.ErrorCode.STT_FAILED, str(e)))
            finally:
                self.handling = False

    async def _wake_check(self, seg: Segment, epoch: int):
        if self.ack_from is not None and seg.start < self.ack_from:
            # "Mimi ơi … EC2 là gì": ngập ngừng sau tên làm VAD cắt hai đoạn. Đoạn này bắt đầu trước khi
            # câu đáp phát ra nên không thể là tiếng loa: là câu hỏi
            self.ack_from = None
            self.speak_seq += 1
            self.s.stop_speaking()
            return await self._question(seg, epoch)
        if self.s.speak_task and not self.s.speak_task.done():
            return  # đang đọc: không nhận dạng để GPU dành cho TTS; ngắt lời bằng Space / 🎤
        window_ms = round(self.cfg.assistant_wake_window_s * 1000)
        text, stt_ms = await self.s.run_gpu(self.s.stt.transcribe_plain, seg.audio[: window_ms * SR // 1000],
                                           self.cfg.assistant_wake_prompt)
        if epoch != self.epoch or self.s.state is State.LISTENING:
            return  # đã reset, hoặc người dùng đang giữ Space
        m = self.matcher.match(text)
        action = decide(m, seg.audio_ms, window_ms, self.cfg.assistant_min_rest_words)
        direct = action == "ask"
        ack = self._pick_ack() if action == "ack" else None
        self.s.turnlog.wake(seg.audio if m.found else None, mode=str(self.mode), text=text, found=m.found,
                            score=m.score, heard=m.heard, action=action, audio_ms=seg.audio_ms,
                            speech_ms=seg.speech_ms, stt_ms=stt_ms)
        await self.s.send(P.heard(text, m.found, action, ack, stt_ms))
        if not m.found:
            return
        self.speak_seq += 1    # lượt đọc cũ bị huỷ dưới đây không được đổi trạng thái nữa
        self.s.stop_asking()   # ngắt lời nếu đang nghĩ/đọc
        self.s.stop_speaking()
        if direct:
            await self._question(seg, epoch)
        elif await self._grace(seg) and epoch == self.epoch:
            await self._ack(ack)

    async def _question(self, seg: Segment, epoch: int):
        self.ack_from = None
        await self.set_mode(ListenMode.BUSY)
        await self.s.set_state(State.TRANSCRIBING)
        res = await self.s.recognize(seg.audio, seg.audio_ms, self.topic, source="handsfree")
        if epoch != self.epoch:
            return
        if res is None:
            await self.s.set_state(State.IDLE)
            return await self.set_mode(ListenMode.SLEEP)
        m = self.matcher.match(res.text)
        text = m.rest if m.found else res.text.strip()
        if not text:  # chỉ gọi tên (lúc đang ACTIVE): đáp lại
            await self.s.set_state(State.IDLE)
            return await self._ack(self._pick_ack())
        if self.s.bridge is None:
            await self.s.set_state(State.IDLE)
            return self._open_window(self.seg.pos, self.cfg.assistant_follow_up_s)
        self.s.start_ask(text, on_spoken=self._after_speaking(self.cfg.assistant_follow_up_s))

    async def _grace(self, seg: Segment) -> bool:
        """Chỉ có tên: chờ ack_grace_ms (tính theo mic) xem người dùng có nói tiếp không. True = đáp.

        Nói tiếp thì chờ đoạn đó cắt xong: đoạn vào hàng đợi sẽ thành câu hỏi (xem _wake_check),
        còn nếu VAD bỏ (tiếng động ngắn) thì vẫn đáp như bình thường.
        """
        self.ack_from = self.seg.pos + self.cfg.assistant_ack_grace_ms * SR // 1000
        await self.set_mode(ListenMode.BUSY)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2 * self.cfg.assistant_ack_grace_ms / 1000  # phòng khi mic ngừng gửi
        while self.seg.pos < self.ack_from and loop.time() < deadline and self.last_start <= seg.start:
            await asyncio.sleep(0.02)
        if self.last_start <= seg.start:
            return True
        deadline = loop.time() + self.cfg.assistant_max_segment_s + 1
        while self.seg.in_speech and loop.time() < deadline:
            await asyncio.sleep(0.02)
        return self.queue.empty()

    async def _ack(self, phrase: str):
        await self.set_mode(ListenMode.BUSY)
        if self.s.tts is None:
            return self._open_window(self.seg.pos, self.cfg.assistant_active_timeout_s)
        queue = self.s.begin_speaking(on_end=self._after_speaking(self.cfg.assistant_active_timeout_s))[1]
        queue.put_nowait(phrase)
        queue.put_nowait(None)

    def _pick_ack(self) -> str:
        acks = [a for a in self.cfg.assistant_acks if a != self.last_ack] or self.cfg.assistant_acks
        self.last_ack = random.choice(acks)
        return self.last_ack

    def _after_speaking(self, seconds: float):
        """Callback khi đọc xong: mở cửa sổ ACTIVE từ lúc (ước lượng) loa phát xong."""
        self.speak_seq += 1
        start, epoch, seq = self.seg.pos, self.epoch, self.speak_seq

        async def done(cancelled: bool, ttfa_ms: int | None, synth_ms: int, audio_ms: int):
            if epoch != self.epoch or seq != self.speak_seq:
                return  # đã reset hoặc bị ngắt lời: luồng mới tự đặt trạng thái
            if cancelled:  # lỗi TTS, hoặc dừng không qua rảnh tay
                return await self.set_mode(ListenMode.SLEEP)
            play_ms = max((ttfa_ms or 0) + audio_ms, synth_ms) + PLAYBACK_MARGIN_MS
            self._open_window(start + play_ms * SR // 1000, seconds)
            await self._tick()

        return done

    def _open_window(self, start: int, seconds: float):
        self.pending = (start, start + round(max(seconds, 0) * SR))
        if self.mode is not ListenMode.BUSY:
            self.mode = ListenMode.BUSY  # _tick chuyển sang ACTIVE khi tới mốc
