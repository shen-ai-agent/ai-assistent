"""Bridge gọi Claude Code headless: claude -p --output-format stream-json, giữ ngữ cảnh bằng --resume.

Chạy trong thư mục làm việc riêng (_local/claude-workdir) và chỉ cho WebSearch/WebFetch, để Claude không
đọc hay sửa code dự án. Thư mục đó vẫn nằm trong repo nên phải tắt CLAUDE.md, git status và memory
qua --settings (đã kiểm: không tắt thì Claude thấy CLAUDE.md của repo).

generate(): lượt claude -p độc lập cho client ngoài (game TikTok Live World), không dùng session của Mimi.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import AsyncIterator

from .base import BridgeError, Done, TextDelta

log = logging.getLogger("bridge")

SETTINGS = {"claudeMdExcludes": ["**/CLAUDE.md"], "includeGitInstructions": False, "autoMemoryEnabled": False}
GENERATE_SLOTS = 2  # số lượt generate chạy cùng lúc, lượt sau xếp hàng
# generate tắt thinking: đo với prompt gắn tag 5 câu (haiku) bật thinking mất 16,5s, tắt còn 2,4s, chi phí ~1/5
GENERATE_SETTINGS = {**SETTINGS, "alwaysThinkingEnabled": False}


def parse_generate_output(stdout: bytes, stderr: bytes, returncode: int | None) -> str:
    """Lấy text từ output của claude -p --output-format json (một object result; bản cũ có thể in mảng)."""
    result = None
    for line in stdout.decode("utf-8", "replace").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        for item in obj if isinstance(obj, list) else [obj]:
            if isinstance(item, dict) and item.get("type") == "result":
                result = item
    if result is None:
        err = stderr.decode("utf-8", "replace").strip()
        log.warning("claude (generate) thoát mã %s: %s", returncode, err[-500:])
        raise BridgeError(err.splitlines()[-1] if err else f"claude thoát mã {returncode}")
    if result.get("is_error") or result.get("subtype") != "success":
        raise BridgeError(str(result.get("result") or result.get("subtype") or "lỗi không rõ"))
    return str(result.get("result") or "")


def load_prompt(path: Path) -> str:
    """Phần sau dòng '---' đầu tiên của prompts/voice_reply.md (phần trước là ghi chú cho người)."""
    text = path.read_text(encoding="utf-8")
    return text.split("\n---\n", 1)[-1].strip()


class StreamParser:
    """Dịch từng dòng stream-json thành sự kiện của bridge. Tách riêng để test không cần claude."""

    def __init__(self):
        self.session_id: str | None = None
        self.result: dict | None = None
        self.model: str | None = None
        self._has_text = False

    def feed(self, obj: dict) -> list[TextDelta]:
        kind = obj.get("type")
        if kind == "system" and obj.get("subtype") == "init":
            self.session_id = obj.get("session_id") or self.session_id
            self.model = obj.get("model") or self.model
        elif kind == "stream_event":
            ev = obj.get("event", {})
            if ev.get("type") == "content_block_start" and ev.get("content_block", {}).get("type") == "text":
                if self._has_text:
                    return [TextDelta("\n")]  # đoạn text mới sau khi Claude dùng tool: tách câu
            elif ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                text = ev["delta"].get("text", "")
                if text:
                    self._has_text = True
                    return [TextDelta(text)]
        elif kind == "result":
            self.result = obj
            self.session_id = obj.get("session_id") or self.session_id
        return []

    def done(self) -> Done:
        r = self.result
        if r is None:
            raise BridgeError("Claude dừng mà không trả kết quả")
        if r.get("is_error") or r.get("subtype") != "success":
            raise BridgeError(str(r.get("result") or r.get("subtype") or "lỗi không rõ"))
        return Done(self.session_id, r.get("total_cost_usd"), self.model)


class ClaudeCliBridge:
    name = "claude_cli"

    def __init__(self, workdir: Path, prompt_path: Path, tools: list[str], model: str = "",
                 timeout_s: float = 120, exe: str | None = None, generate_timeout_s: float = 60):
        self.workdir = workdir
        self.prompt = load_prompt(prompt_path)
        self.tools = tools
        self.model = model
        self.timeout_s = timeout_s
        self.generate_timeout_s = generate_timeout_s
        self.exe = exe or shutil.which("claude")
        self.session_id: str | None = None
        self._lock = asyncio.Lock()  # một session chỉ chạy một lượt một lúc (nhiều tab)
        self._generate_slots = asyncio.Semaphore(GENERATE_SLOTS)  # dùng chung mọi tab, không chặn ask()

    def new_session(self) -> None:
        self.session_id = None

    def command(self, text: str) -> list[str]:
        tools = ",".join(self.tools)
        cmd = [self.exe, "-p", text, "--output-format", "stream-json", "--verbose", "--include-partial-messages",
               "--tools", tools, "--allowedTools", tools, "--strict-mcp-config",
               "--settings", json.dumps(SETTINGS), "--append-system-prompt", self.prompt]
        if self.model:
            cmd += ["--model", self.model]
        if self.session_id:
            # Fork để lượt bị ngắt/lỗi không ghi vào session đang dùng: không fork thì câu hỏi dở dang
            # nằm lại trong session và lượt sau Claude trả lời bù câu đó
            cmd += ["--resume", self.session_id, "--fork-session"]
        return cmd

    async def ask(self, text: str) -> AsyncIterator[TextDelta | Done]:
        if not self.exe:
            raise BridgeError("Không tìm thấy lệnh claude. Cài Claude Code và đăng nhập trước.")
        async with self._lock:
            self.workdir.mkdir(parents=True, exist_ok=True)
            # Không qua shell: cmd.exe làm hỏng prompt có nhiều dòng
            proc = await asyncio.create_subprocess_exec(
                *self.command(text), cwd=self.workdir, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=16 * 2**20)
            parser = StreamParser()
            try:
                async with asyncio.timeout(self.timeout_s):
                    async for line in proc.stdout:
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue
                        for ev in parser.feed(obj):
                            yield ev
                    await proc.wait()
            except TimeoutError:
                raise BridgeError(f"Claude không trả lời xong sau {self.timeout_s:.0f} giây") from None
            finally:
                if proc.returncode is None:
                    proc.kill()  # bị huỷ (người dùng dừng / nói câu mới) hoặc timeout
                    await proc.wait()
            if parser.result is None:
                err = (await proc.stderr.read()).decode("utf-8", "replace").strip()
                log.warning("claude thoát mã %s: %s", proc.returncode, err[-500:])
                raise BridgeError(err.splitlines()[-1] if err else f"claude thoát mã {proc.returncode}")
            done = parser.done()
            self.session_id = done.session_id  # chỉ lượt trả lời xong mới thành ngữ cảnh của lượt sau
            yield done

    def generate_command(self, system: str, model: str) -> list[str]:
        # Không --resume, không tool, không lưu session; system thay hẳn prompt mặc định của Claude Code
        return [self.exe, "-p", "--output-format", "json", "--tools", "", "--strict-mcp-config",
                "--settings", json.dumps(GENERATE_SETTINGS), "--system-prompt", system, "--model", model,
                "--no-session-persistence", "--disable-slash-commands"]

    async def generate(self, system: str, text: str, model: str) -> str:
        """Một lượt hỏi-đáp độc lập, trả text Claude viết. Không đụng session_id và model của Mimi.

        text đi qua stdin (Windows giới hạn dòng lệnh ~32K ký tự). Huỷ task = kill tiến trình claude.
        """
        if not self.exe:
            raise BridgeError("Không tìm thấy lệnh claude. Cài Claude Code và đăng nhập trước.")
        async with self._generate_slots:
            self.workdir.mkdir(parents=True, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                *self.generate_command(system, model), cwd=self.workdir, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                async with asyncio.timeout(self.generate_timeout_s):
                    stdout, stderr = await proc.communicate(text.encode("utf-8"))
            except TimeoutError:
                raise BridgeError(f"Claude không trả lời xong sau {self.generate_timeout_s:.0f} giây") from None
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
        return parse_generate_output(stdout, stderr, proc.returncode)
