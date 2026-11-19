"""Đổi model TTS khi server đang chạy (tab Cài đặt của web).

Cả server dùng chung một model TTS nên đổi là đổi cho mọi tab. Trình tự: báo mọi tab (web hiện lớp chờ chặn
thao tác), dừng mọi lượt hỏi/đọc, chờ câu đang tổng hợp xong (khoá TTS), tắt model cũ để trả VRAM, nạp model
mới, chạy thử một câu. Lỗi thì nạp lại model cũ. Trong lúc đổi, Session từ chối message cần TTS/STT
(xem BLOCKED) và bỏ frame mic rảnh tay.

Model đã chọn ghi vào [server] settings_file (_local/settings.json) để lần khởi động sau dùng tiếp, không sửa
config.toml. Model đã lưu không nạp được thì quay về [tts] engine trong config.
"""

from __future__ import annotations

import asyncio
import dataclasses
import gc
import json
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable

from . import protocol as P
from .config import ROOT, Config
from .tts.synth import TtsService

log = logging.getLogger("server")

# Message client bị từ chối khi đang đổi model (cần TTS, hoặc sinh câu trả lời sẽ được đọc)
BLOCKED = frozenset({"start", "speak", "ask", "tts_switch"})


def load_settings(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        log.warning("Không đọc được %s, bỏ qua", path)
        return {}


def save_settings(path: Path | None, **values) -> None:
    if path is None:
        return
    data = load_settings(path) | values
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def engine_label(cfg: Config, name: str) -> str:
    return cfg.tts_engines.get(name, {}).get("label") or name


def engine_list(cfg: Config) -> list[dict]:
    """Các engine có bảng [tts.<tên>] trong config, cho ô chọn ở tab Cài đặt.
    available = false khi bảng có python mà venv chưa cài (web hiện nhưng không cho chọn)."""
    items = []
    for name, opts in cfg.tts_engines.items():
        available, note = True, "chạy trong server"
        if opts.get("url"):
            note = f"worker chạy sẵn ở {opts['url']}"
        elif opts.get("python"):
            python = Path(opts["python"])
            if not (python if python.is_absolute() else ROOT / python).exists():
                available, note = False, f"chưa cài (thiếu {opts['python']})"
            else:
                note = "chạy trong venv riêng (worker)"
        items.append({"name": name, "label": engine_label(cfg, name), "available": available, "note": note})
    return items


def tts_info(cfg: Config, tts: TtsService | None) -> dict | None:
    """hello.tts / tts_switch.tts: thông tin model đang dùng (None = không có TTS)."""
    if tts is None:
        return None
    return {"sample_rate": tts.sample_rate, "voice": getattr(tts.engine, "voice", ""), "engine": cfg.tts_engine,
            "label": engine_label(cfg, cfg.tts_engine)}


def close_engine(tts: TtsService | None) -> None:
    """Tắt model (worker thì tắt tiến trình) và thu dọn để trả VRAM trước khi nạp model khác."""
    if tts is not None and hasattr(tts.engine, "close"):
        tts.engine.close()
    gc.collect()


class TtsSwitcher:
    """Giữ model TTS đang dùng (state.tts) và đổi model theo yêu cầu của web.

    state: app.state (có tts, tts_lock, sessions). factory(cfg) → engine. broadcast(msg) gửi mọi tab.
    """

    def __init__(self, cfg: Config, state, factory: Callable[[Config], object],
                 broadcast: Callable[[dict], Awaitable[None]]):
        self.cfg, self.state, self.factory, self.broadcast = cfg, state, factory, broadcast
        self.job: dict | None = None   # lần đổi đang chạy (message tts_switch gần nhất), None = không đổi

    @property
    def busy(self) -> bool:
        return self.job is not None

    def create(self, name: str) -> TtsService:
        """Nạp engine (blocking, gọi trong thread)."""
        cfg = dataclasses.replace(self.cfg, tts_engine=name)
        return TtsService(self.factory(cfg), loanwords=cfg.tts_loanwords, pause_scale=cfg.tts_pause_scale)

    @staticmethod
    def warmup(tts: TtsService) -> None:
        """Đọc thử một câu (blocking). Lần chạy đầu của onnxruntime CUDA chậm (cấp phát, chọn kernel);
        lỗi thì tắt engine vừa nạp rồi ném lại."""
        try:
            for seg in tts.plan("Xin chào bạn."):
                for _ in tts.stream(seg):
                    pass
        except Exception:
            close_engine(tts)
            raise
        describe = getattr(tts.engine, "describe", None)
        log.info("TTS sẵn sàng: %s, giọng %s (%s)", type(tts.engine).__name__, getattr(tts.engine, "voice", ""),
                 describe() if describe else "")

    def load(self, name: str) -> TtsService:
        tts = self.create(name)
        self.warmup(tts)
        return tts

    async def start(self) -> None:
        """Lúc khởi động server: model đã lưu ở settings_file, không nạp được thì model trong config."""
        saved = load_settings(self.cfg.settings_file).get("tts_engine")
        if saved and saved != self.cfg.tts_engine and saved in self.cfg.tts_engines:
            log.info("Đang load TTS %s (đã chọn ở tab Cài đặt)...", saved)
            try:
                self.state.tts = await asyncio.to_thread(self.load, saved)
                self.cfg.tts_engine = saved
                return
            except Exception:
                log.exception("Không nạp được TTS %s, dùng %s trong config", saved, self.cfg.tts_engine)
        log.info("Đang load TTS %s...", self.cfg.tts_engine)
        self.state.tts = await asyncio.to_thread(self.load, self.cfg.tts_engine)

    def close(self) -> None:
        close_engine(self.state.tts)
        self.state.tts = None

    def check(self, name) -> str | None:
        """Lý do không đổi được (None = đổi được)."""
        if self.busy:
            return "Đang đổi model, chờ xong đã"
        item = next((e for e in engine_list(self.cfg) if e["name"] == name), None)
        if item is None:
            return f"Không có engine TTS {name!r} trong server/config.toml"
        if not item["available"]:
            return f"{item['label']}: {item['note']}"
        return None

    async def switch(self, name: str) -> bool:
        """Đổi sang engine name. Trả True nếu xong; gửi tts_switch cho mọi tab ở mỗi bước."""
        if (reason := self.check(name)) is not None:
            raise ValueError(reason)
        old_name = self.cfg.tts_engine
        steps = ["Dừng câu đang đọc", f"Tắt {engine_label(self.cfg, old_name)}",
                 f"Nạp {engine_label(self.cfg, name)}", "Đọc thử"]
        self.job = {"engine": name, "label": engine_label(self.cfg, name), "from": old_name, "steps": steps,
                    "step": 0, "status": "running", "error": None, "started": time.time()}
        await self._report()
        try:
            for s in list(self.state.sessions):
                await s.halt_for_switch()
            async with self.state.tts_lock:  # câu đang tổng hợp dở xong rồi mới tắt model
                try:
                    await self._step(1)
                    old, self.state.tts = self.state.tts, None
                    await asyncio.to_thread(close_engine, old)
                    await self._step(2)
                    tts = await asyncio.to_thread(self.create, name)
                    await self._step(3)
                    await asyncio.to_thread(self.warmup, tts)
                except Exception as e:
                    log.exception("Không nạp được TTS %s", name)
                    return await self._restore(old_name, str(e) or type(e).__name__)
                self.state.tts = tts
            self.cfg.tts_engine = name
            save_settings(self.cfg.settings_file, tts_engine=name)
            self.job.update(status="done", step=len(steps))
            await self._report()
            return True
        except Exception as e:  # lỗi ngoài dự kiến: web vẫn phải được báo để gỡ lớp chờ
            log.exception("Đổi TTS lỗi")
            self.job.update(status="failed", error=str(e) or type(e).__name__)
            await self._report()
            return False
        finally:
            self.job = None

    async def _restore(self, old_name: str, error: str) -> bool:
        """Model mới lỗi: nạp lại model cũ để Mimi vẫn đọc được."""
        self.job["failed_step"] = self.job["step"]
        self.job["steps"].append(f"Nạp lại {engine_label(self.cfg, old_name)}")
        await self._step(len(self.job["steps"]) - 1)
        try:
            self.state.tts = await asyncio.to_thread(self.load, old_name)
        except Exception as e:
            log.exception("Không nạp lại được TTS %s", old_name)
            error += f". Nạp lại {old_name} cũng lỗi: {e}. Server đang không đọc được, khởi động lại server."
        self.job.update(status="failed", error=error)
        await self._report()
        return False

    async def _step(self, i: int):
        self.job["step"] = i
        await self._report()

    def status(self) -> dict | None:
        """Lần đổi đang chạy, cho hello (tab mở giữa lúc đổi cũng hiện lớp chờ)."""
        if self.job is None:
            return None
        return {k: v for k, v in self.job.items() if k != "started"} | {
            "elapsed_s": round(time.time() - self.job["started"], 1)}

    async def _report(self):
        await self.broadcast(P.tts_switch(self.status(), tts_info(self.cfg, self.state.tts), engine_list(self.cfg)))
