"""Theo dõi log phiên Claude Code (~/.claude/projects/<slug>/<sessionId>.jsonl) sau khi giao việc (T-011).

Dò phiên chứa prompt vừa gửi, rồi đọc tăng dần file đó để báo: tool đang chạy, tool chờ lâu (chờ duyệt hoặc
lệnh chạy lâu: log không phân biệt được), xong chưa và câu trả lời cuối.

Đặc điểm log (đã xem log thật do VS Code ghi):
- Một message API bị tách thành nhiều dòng `assistant`, mỗi dòng một content block, cùng `message.id` và cùng
  `stop_reason`. Thấy `end_turn` thì chờ đến khi có dòng thuộc message khác hoặc `settle_s` không có dòng mới.
- Kết quả tool là dòng `user` có block `tool_result`. Tool chờ duyệt thì không ghi gì: chỉ thấy tool_use.
- Bấm dừng: dòng `user` có text bắt đầu bằng "[Request interrupted by user".
- Bản ghi `isMeta` (skill chèn vào), `isSidechain` (subagent) và các loại khác bị bỏ qua.
- File đang ghi: dòng cuối có thể chưa đủ, giữ lại đợi lần đọc sau.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Callable

from .projects import claude_project_dirs, claude_slug

log = logging.getLogger("agent")

PROJECTS_ROOT = Path.home() / ".claude" / "projects"
SLACK_S = 5.0                # lệch đồng hồ cho phép giữa `since` và timestamp/mtime của log
TAIL_BYTES = 512 * 1024      # file cũ lớn khi dò: chỉ đọc phần cuối (prompt mới nằm ở cuối)
SUMMARY_MAX = 120
INTERRUPT_MARK = "[Request interrupted by user"
FINAL_STOPS = {"end_turn", "stop_sequence", "refusal"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SUMMARY_KEYS = ("command", "file_path", "notebook_path", "pattern", "url", "query", "description", "skill",
                "prompt", "path")


@dataclass
class Submitted:
    session_id: str
    file: Path


@dataclass
class ToolStep:
    tool_use_id: str
    tool: str
    summary: str


@dataclass
class ToolDone:
    tool_use_id: str
    ok: bool


@dataclass
class Waiting:
    step: ToolStep


@dataclass
class Finished:
    session_id: str
    text: str
    tools: int
    files: list[str]
    interrupted: bool = False


@dataclass
class NotSubmitted:
    pass


Event = Submitted | ToolStep | ToolDone | Waiting | Finished | NotSubmitted


# ---------------------------------------------------------------------------------------------------------
# Hàm phụ


def project_slug(path: Path | str) -> str:
    """Tên thư mục log của Claude Code: mọi ký tự không phải chữ/số thành '-'."""
    return claude_slug(Path(path))


def project_log_dirs(path: Path | str, root: Path = PROJECTS_ROOT) -> list[Path]:
    """Các thư mục log của dự án đang có (không phân biệt hoa thường, có thể có hai thư mục).
    Dùng làm `dirs` cho SessionWatcher: `lambda: project_log_dirs(p)`."""
    return claude_project_dirs(Path(path), root)


def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def prompt_matches(text: str, prompt: str) -> bool:
    """Khớp mềm (người dùng có thể sửa prompt trước khi Enter): bằng nhau sau khi gộp khoảng trắng, hoặc
    chung 40 ký tự đầu, hoặc difflib ratio ≥ 0.8."""
    a, b = _norm(text), _norm(prompt)
    if not a or not b:
        return False
    if a == b or (len(a) >= 40 and len(b) >= 40 and a[:40] == b[:40]):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def _parse_ts(ts) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _blocks(rec: dict) -> list:
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def _texts(rec: dict) -> list[str]:
    return [b.get("text", "") for b in _blocks(rec) if isinstance(b, dict) and b.get("type") == "text"]


def _is_prompt_record(rec: dict) -> bool:
    """Bản ghi user do người gõ: không isMeta, không sidechain, không chứa tool_result."""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return False
    blocks = _blocks(rec)
    return bool(blocks) and not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks)


def _prompt_candidates(rec: dict) -> list[str]:
    """Text để so với prompt: từng block và cả cụm (bỏ block ngữ cảnh IDE kiểu <ide_opened_file>)."""
    texts = [t for t in _texts(rec) if t.strip() and not t.lstrip().startswith("<ide_")]
    return texts + (["\n".join(texts)] if len(texts) > 1 else [])


def summarize(tool: str, inp) -> str:
    """Mô tả ngắn (≤120 ký tự) của một tool_use. MCP tool "mcp__x__y" → "x: y"."""
    if tool.startswith("mcp__"):
        parts = tool.split("__", 2)
        s = f"{parts[1]}: {parts[2]}" if len(parts) == 3 else tool
    else:
        s = ""
        if isinstance(inp, dict):
            s = next((inp[k] for k in SUMMARY_KEYS if isinstance(inp.get(k), str) and inp[k].strip()), "")
    s = " ".join(s.split())
    return s if len(s) <= SUMMARY_MAX else s[:SUMMARY_MAX - 1] + "…"


class JsonlTail:
    """Đọc tăng dần một file jsonl theo offset; giữ lại dòng cuối chưa có '\\n'."""

    def __init__(self, path: Path, offset: int = 0):
        # Bắt đầu giữa file: lùi một byte rồi bỏ đoạn đến '\n' đầu tiên. Nếu offset đúng đầu dòng thì đoạn bỏ đi
        # rỗng, không mất dòng nào.
        self.start = offset  # vị trí yêu cầu, dùng lại để mở JsonlTail khác cùng chỗ
        offset = max(0, offset - 1)
        self.path = path
        self.offset = offset
        self._skip_partial = offset > 0

    def read(self) -> list[dict]:
        try:
            with open(self.path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                if size < self.offset:  # file bị ghi lại từ đầu
                    self.offset, self._skip_partial = 0, False
                if size == self.offset:
                    return []
                f.seek(self.offset)
                data = f.read(size - self.offset)
        except OSError:
            return []
        end = data.rfind(b"\n")
        if end < 0:
            return []
        chunk = data[:end]
        self.offset += end + 1
        lines = chunk.split(b"\n")
        if self._skip_partial:
            lines, self._skip_partial = lines[1:], False
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out


@dataclass
class _Turn:
    """Trạng thái một lượt sau prompt."""

    session_id: str
    tools: int = 0
    files: list[str] = field(default_factory=list)
    pending: dict[str, tuple[ToolStep, float]] = field(default_factory=dict)
    waited: set[str] = field(default_factory=set)
    all_texts: list[str] = field(default_factory=list)
    msg_id: str | None = None
    msg_texts: list[str] = field(default_factory=list)
    msg_stop: str | None = None
    last_line: float = 0.0


# ---------------------------------------------------------------------------------------------------------


class SessionWatcher:
    """events(): Submitted → (ToolStep | ToolDone | Waiting)* → Finished, hoặc NotSubmitted.

    Polling theo poll_s; `clock` tiêm được để test. Người gọi huỷ bằng cách đóng generator / huỷ task."""

    def __init__(self, dirs: Callable[[], list[Path]], prompt: str, since: float, *, poll_s: float = 0.5,
                 stall_s: float = 12.0, submit_timeout_s: float = 600.0, settle_s: float = 1.5,
                 clock: Callable[[], float] = time.time):
        self.dirs = dirs
        self.prompt = prompt
        self.since = since
        self.poll_s = poll_s
        self.stall_s = stall_s
        self.submit_timeout_s = submit_timeout_s
        self.settle_s = settle_s
        self.clock = clock
        self._scan: dict[Path, JsonlTail] = {}

    async def events(self) -> AsyncIterator[Event]:
        start = self.clock()
        found = None
        while found is None:
            found = self._search()
            if found:
                break
            if self.clock() - start >= self.submit_timeout_s:
                yield NotSubmitted()
                return
            await asyncio.sleep(self.poll_s)

        file, rec = found
        session_id = str(rec.get("sessionId") or file.stem)
        yield Submitted(session_id, file)

        # Đọc lại từ chỗ bắt đầu dò (đầu file nếu file mới) để không mất sự kiện ghi trước khi tìm thấy
        tail = JsonlTail(file, self._scan[file].start)
        turn = _Turn(session_id, last_line=self.clock())
        prompt_uuid = rec.get("uuid")
        started = False
        while True:
            for r in tail.read():
                if not started:
                    # Chỉ xét bản ghi sau prompt
                    started = (r.get("uuid") == prompt_uuid) if prompt_uuid else (r is rec or r == rec)
                    continue
                for ev in self._feed(turn, r):
                    yield ev
                    if isinstance(ev, Finished):
                        return
            now = self.clock()
            if turn.msg_stop in FINAL_STOPS and now - turn.last_line >= self.settle_s:
                yield self._finished(turn)
                return
            for tid, (step, seen) in list(turn.pending.items()):
                if tid not in turn.waited and now - seen >= self.stall_s:
                    turn.waited.add(tid)
                    yield Waiting(step)
            await asyncio.sleep(self.poll_s)

    # --- dò phiên -----------------------------------------------------------------------------------

    def _search(self) -> tuple[Path, dict] | None:
        """Tìm bản ghi prompt trong các *.jsonl mới sửa. Mỗi file đọc tăng dần; file lớn có sẵn thì chỉ đọc
        từ TAIL_BYTES cuối, nên mỗi lần poll chỉ tốn phần mới ghi thêm."""
        lo = self.since - SLACK_S
        try:
            dirs = list(self.dirs())
        except Exception:
            log.exception("dirs() lỗi")
            return None
        for d in dirs:
            try:
                files = [p for p in d.glob("*.jsonl") if p.stat().st_mtime >= lo]
            except OSError:
                continue
            for p in sorted(files, key=lambda p: p.stat().st_mtime if p.exists() else 0):
                tail = self._scan.get(p)
                if tail is None:
                    try:
                        size = p.stat().st_size
                    except OSError:
                        continue
                    tail = self._scan[p] = JsonlTail(p, max(0, size - TAIL_BYTES))
                for rec in tail.read():
                    if not _is_prompt_record(rec):
                        continue
                    ts = _parse_ts(rec.get("timestamp"))
                    if ts is None or ts < lo:
                        continue
                    if any(prompt_matches(t, self.prompt) for t in _prompt_candidates(rec)):
                        return p, rec
        return None

    # --- theo dõi lượt ------------------------------------------------------------------------------

    def _finished(self, turn: _Turn, interrupted: bool = False) -> Finished:
        texts = turn.all_texts if interrupted else turn.msg_texts
        text = "\n\n".join(t.strip() for t in texts if t.strip())
        return Finished(turn.session_id, text, turn.tools, list(turn.files), interrupted)

    def _feed(self, turn: _Turn, rec: dict) -> list[Event]:
        kind = rec.get("type")
        if kind not in ("user", "assistant") or rec.get("isSidechain"):
            return []
        out: list[Event] = []
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        msg_id = msg.get("id") or rec.get("requestId")
        # Đã có end_turn mà gặp dòng thuộc message khác: message trước là cuối
        if turn.msg_stop in FINAL_STOPS and (kind != "assistant" or msg_id != turn.msg_id):
            return [self._finished(turn)]
        turn.last_line = self.clock()

        if kind == "assistant":
            if msg_id != turn.msg_id:
                turn.msg_id, turn.msg_texts, turn.msg_stop = msg_id, [], None
            if msg.get("stop_reason"):
                turn.msg_stop = msg["stop_reason"]
            for b in _blocks(rec):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text"):
                    turn.msg_texts.append(b["text"])
                    turn.all_texts.append(b["text"])
                elif b.get("type") == "tool_use":
                    name = str(b.get("name") or "")
                    inp = b.get("input")
                    step = ToolStep(str(b.get("id") or ""), name, summarize(name, inp))
                    turn.tools += 1
                    turn.pending[step.tool_use_id] = (step, self.clock())
                    if name in EDIT_TOOLS and isinstance(inp, dict):
                        fp = inp.get("file_path") or inp.get("notebook_path")
                        if isinstance(fp, str) and fp and fp not in turn.files:
                            turn.files.append(fp)
                    out.append(step)
            return out

        # user: kết quả tool, hoặc dấu bấm dừng
        if rec.get("isMeta"):
            return out
        for b in _blocks(rec):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                tid = str(b.get("tool_use_id") or "")
                turn.pending.pop(tid, None)
                out.append(ToolDone(tid, not b.get("is_error")))
            elif b.get("type") == "text" and str(b.get("text", "")).lstrip().startswith(INTERRUPT_MARK):
                out.append(self._finished(turn, interrupted=True))
                return out
        return out
