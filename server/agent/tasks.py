"""Điều phối việc giao cho VS Code (T-011): tách câu → tìm dự án → mở VS Code, điền yêu cầu → theo dõi log phiên.

Task thuộc cả máy chứ không thuộc một tab: mọi tab thấy cùng danh sách (broadcast), tải lại trang vẫn thấy.
Ở đây không gọi Claude: Claude chạy trong VS Code, server chỉ đọc log phiên nó ghi ra (watcher.py).
Thông báo bằng giọng chỉ ba lúc: chờ bấm Enter, chờ duyệt (một lần mỗi task), xong việc.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Awaitable, Callable

from ..bridge.asklog import AskLogger
from ..assistant.wake import fold
from .intent import DevTask, find_project_anywhere, guess_open, parse_dev_task, pick_project
from .projects import Project, ProjectRegistry, claude_project_dirs
from .vscode import VsCodeLauncher
from .watcher import Finished, NotSubmitted, SessionWatcher, Submitted, ToolDone, ToolStep, Waiting

log = logging.getLogger("agent")

END_STATUSES = ("done", "failed", "cancelled", "interrupted")
MAX_TASKS = 30          # giữ chừng này task gần nhất để hiện trên web
REFRESH_S = 30          # quét lại thư mục dự án nếu lần quét trước cũ hơn chừng này
_FENCE = re.compile(r"```.*?(```|$)", re.S)
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_WORD = re.compile(r"\w+")


@dataclass
class Step:
    step_id: int
    tool: str
    summary: str
    ok: bool | None = None


@dataclass
class Task:
    task_id: int
    project: Project | None
    request: str
    source: str                       # "voice" | "text"
    started_at: float
    query: str = ""                   # tên dự án nghe được (để báo khi không tìm thấy)
    candidates: list[Project] = field(default_factory=list)
    status: str = "opening"
    detail: str | None = None
    steps: list[Step] = field(default_factory=list)
    report: str | None = None
    files: list[str] = field(default_factory=list)
    tools: int = 0
    duration_ms: int | None = None
    session_id: str | None = None
    acked: bool = False               # câu đáp lúc nhận việc (ack_text) đã được đọc
    raw: str = ""                     # câu nói gốc (để ghi nhãn khi người dùng sửa dự án)
    turn_id: str | None = None        # lượt nhận dạng của câu nói gốc, None nếu gõ tay
    origin: object | None = field(default=None, repr=False)   # Session giao việc, để đọc thông báo ở đúng tab
    runner: asyncio.Task | None = field(default=None, repr=False)

    @property
    def finished(self) -> bool:
        return self.status in END_STATUSES

    def project_dict(self) -> dict | None:
        return self.project and {"name": self.project.name, "path": str(self.project.path)}

    def to_dict(self) -> dict:
        return {"task_id": self.task_id, "project": self.project_dict(), "request": self.request,
                "source": self.source, "status": self.status, "detail": self.detail,
                "steps": [s.__dict__ for s in self.steps], "report": self.report, "files": self.files,
                "tools": self.tools, "duration_ms": self.duration_ms, "session_id": self.session_id,
                "started_at": self.started_at}


def spoken_name(project: Project) -> str:
    """Tên đọc được: "job-hay" → "job hay", "Shen/agentic-comics" → "Shen agentic comics"."""
    return re.sub(r"[-_/\\]+", " ", project.name).strip()


def ack_text(task: Task) -> str:
    """Câu Mimi đáp ngay khi nhận việc."""
    if task.project is None:
        if task.candidates:
            return ask_project_text(task.candidates)
        return f"Mình không tìm thấy dự án {task.query}." if task.query else "Bạn muốn mở dự án nào?"
    if not task.request:
        return f"Mình mở dự án {spoken_name(task.project)} trong VS Code."
    return f"Mình mở dự án {spoken_name(task.project)} và giao việc cho Claude trong VS Code."


def first_sentences(text: str, max_chars: int = 300) -> str:
    """Vài câu đầu của câu trả lời để đọc: bỏ khối code, gom câu tới khi đủ max_chars."""
    text = _FENCE.sub(" ", text)
    lines = [re.sub(r"^\s*(#+|[-*+]|\d+[.)])\s+", "", ln).strip() for ln in text.splitlines()]
    flat = " ".join(ln for ln in lines if ln)
    out = ""
    for sent in _SENTENCE_END.split(flat):
        if out and len(out) + len(sent) > max_chars:
            break
        out = f"{out} {sent}".strip()
    return out[: max_chars * 2]


def spoken_report(task: Task) -> str | None:
    if task.project is None or task.status == "cancelled":
        return None
    name = spoken_name(task.project)
    if task.status == "done":
        if not task.request:
            return None   # chỉ mở dự án: câu đáp lúc nhận việc đã nói rồi
        files = f" Claude đã sửa {len(task.files)} file." if task.files else ""
        body = first_sentences(task.report or "")
        return f"Xong việc ở dự án {name}.{files} {body}".strip()
    if task.status == "interrupted":
        return f"Việc ở dự án {name} đã bị dừng giữa chừng."
    return f"Việc ở dự án {name} không xong: {task.report}"


def ask_project_text(candidates: list[Project]) -> str:
    if len(candidates) == 1:   # trả lời "ừ" / "không" là đủ
        return f"Mình chưa chắc, có phải dự án {spoken_name(candidates[0])} không?"
    names = " hay ".join(spoken_name(p) for p in candidates[:3])
    return f"Mình chưa chắc là dự án nào: {names}? Bạn nói tên hoặc nói cái thứ mấy nhé."


# ---------- Câu hỏi về việc: "tiến độ sao rồi", "ngừng theo dõi" ----------

def _folded(text: str) -> str:
    return " ".join(fold(w) for w in _WORD.findall(text))


def _fold_all(*phrases: str) -> tuple[str, ...]:
    return tuple(_folded(p) for p in phrases)


_STATUS_HEAD = _fold_all("tiến độ", "báo cáo tiến độ", "status", "progress")
# Chỉ đuôi có "rồi"/"chưa": "sửa lỗi này thế nào" là việc, "việc tới đâu rồi" mới là hỏi tiến độ
_STATUS_TAIL = _fold_all("xong chưa", "sao rồi", "thế nào rồi", "ra sao rồi", "tới đâu rồi", "đến đâu rồi",
                         "đang làm gì", "đang làm gì vậy", "đang làm gì thế")
_CANCEL = _fold_all("ngừng theo dõi", "thôi theo dõi", "bỏ theo dõi", "huỷ việc", "stop tracking")
MAX_QUERY_WORDS = 7     # câu dài hơn là việc giao, không phải câu hỏi tiến độ
PENDING_S = 90          # Mimi hỏi "dự án nào?": câu trả lời tên dự án trong chừng này giây mới tính
DENY_S = 120            # "sai rồi" chỉ tính cho việc giao trong chừng này giây
STT_NAMES = 30          # số dự án dùng gần nhất đưa vào prompt STT ở tab Công việc

# ---------- Trả lời ngắn khi Mimi hỏi lại: "ừ", "cái thứ hai", "không, job hay cơ" ----------

_YES = _fold_all("ừ", "ờ", "ừm", "ừ đúng", "đúng", "đúng rồi", "đúng vậy", "đúng đó", "ừ đúng rồi", "phải",
                 "phải rồi", "chính nó", "chính xác", "cái đó", "nó đó", "ok", "oke", "okay", "yes", "yeah", "yep",
                 "chuẩn", "chuẩn rồi", "vâng", "dạ đúng", "dạ phải", "được", "ừ cái đó")   # "dạ" bỏ dấu = "đã"
_NO = _fold_all("không", "không phải", "không phải đâu", "không đúng", "sai", "sai rồi", "nhầm", "nhầm rồi",
                "sai dự án", "nhầm dự án", "không phải dự án đó", "không phải cái đó", "dự án khác", "no", "nope",
                "wrong", "wrong project")
_ORDINALS = (
    _fold_all("thứ nhất", "cái thứ nhất", "số một", "số 1", "cái đầu", "cái đầu tiên", "đầu tiên", "first"),
    _fold_all("thứ hai", "cái thứ hai", "số hai", "số 2", "cái sau", "cái thứ 2", "second"),
    _fold_all("thứ ba", "cái thứ ba", "số ba", "số 3", "cái cuối", "cái thứ 3", "third"),
)
_REPLY_FILLER = set(_fold_all("à", "ơi", "nhé", "nha", "đi", "đó", "ạ", "mà", "luôn", "chứ", "thế", "vậy", "nhỉ",
                              "là", "cơ", "Mimi"))
_REPLIES: list[tuple[tuple[str, ...], str | int]] = sorted(
    [(tuple(p.split()), "yes") for p in _YES] + [(tuple(p.split()), "no") for p in _NO]
    + [(tuple(p.split()), i) for i, group in enumerate(_ORDINALS) for p in group],
    key=lambda x: -len(x[0]))   # cụm dài trước: "không phải" thắng "không"


def reply_kind(text: str) -> tuple[str | int | None, str]:
    """Câu bắt đầu bằng một câu trả lời ngắn → (kiểu, phần còn lại của câu gốc).
    Kiểu: "yes" | "no" | số thứ tự ứng viên (0, 1, 2) | None. Phần còn lại "" nếu chỉ còn từ đệm.
    "ừ đúng rồi" → ("yes", ""); "không, job hay cơ" → ("no", "job hay cơ"); "job hay" → (None, "job hay")."""
    words = list(_WORD.finditer(text))
    folded = [fold(w.group()) for w in words]
    found, i = None, 0
    while True:   # "ừm, đúng rồi đó": nhiều cụm cùng kiểu liền nhau tính là một
        while i < len(folded) and folded[i] in _REPLY_FILLER:
            i += 1
        hit = next(((p, k) for p, k in _REPLIES if tuple(folded[i:i + len(p)]) == p
                    and (found is None or k == found)), None)
        if hit is None:
            break
        found, i = hit[1], i + len(hit[0])
    if found is None:
        return None, text.strip()
    return found, (text[words[i].start():].strip() if i < len(words) else "")


@dataclass
class Pending:
    """Mimi vừa hỏi lại tên dự án, đang chờ câu trả lời."""
    request: str                          # yêu cầu chờ tên dự án ("" = chỉ mở dự án)
    heard: str = ""                       # câu gốc khiến Mimi hỏi lại (để ghi nhãn)
    query: str = ""                       # phần tên dự án nghe được trong câu gốc
    candidates: list[Project] = field(default_factory=list)   # ứng viên Mimi đã đọc, theo thứ tự
    wrong: Project | None = None          # dự án đã giao nhầm (người dùng nói "sai rồi")
    turn_id: str | None = None            # lượt nhận dạng của câu gốc
    at: float = field(default_factory=time.monotonic)

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.at >= PENDING_S

    def again(self, **changes) -> "Pending":
        """Hỏi lại lần nữa: giữ câu gốc, tính lại hạn chờ."""
        return replace(self, at=time.monotonic(), **changes)


def match_work_query(text: str) -> str | None:
    """"status" / "cancel" nếu câu ngắn hỏi tiến độ / ngừng theo dõi, không thì None."""
    s = _folded(text)
    if not s or len(s.split()) > MAX_QUERY_WORDS:
        return None
    padded = f" {s} "
    if any(f" {p} " in padded for p in _CANCEL):
        return "cancel"
    if any(s == p or s.startswith(p + " ") for p in _STATUS_HEAD) or any(s.endswith(" " + p) or s == p
                                                                         for p in _STATUS_TAIL):
        return "status"
    return None


def status_text(task: Task | None) -> str:
    """Câu đọc khi người dùng hỏi tiến độ."""
    if task is None:
        return "Chưa có việc nào."
    if task.project is None:
        return ack_text(task)
    name = spoken_name(task.project)
    if task.status == "opening":
        return f"Mình đang mở dự án {name} trong VS Code."
    if task.status == "prefilled":
        return f"Yêu cầu ở dự án {name} đã điền sẵn, bạn bấm Enter trong VS Code để gửi nhé."
    if task.status == "waiting":
        return f"Claude ở dự án {name} đang chờ bạn duyệt, hoặc đang chạy lệnh lâu."
    if task.status == "running":
        last = f" Bước gần nhất: {task.steps[-1].tool} {task.steps[-1].summary}." if task.steps else ""
        return f"Claude đang làm ở dự án {name}, đã qua {len(task.steps)} bước.{last}"
    if task.status == "cancelled":
        return f"Mình đã thôi theo dõi việc ở dự án {name}."
    return spoken_report(task) or f"Đã mở dự án {name}."


@dataclass
class Route:
    """Cách xử lý một câu nói: giao việc (dev), hoặc chỉ đọc reply.
    pending: Mimi hỏi lại tên dự án, câu sau là câu trả lời. turn_id: lượt nói gốc của việc (gắn vào task)."""
    dev: DevTask | None = None
    reply: str | None = None
    pending: Pending | None = None
    turn_id: str | None = None

    @property
    def choices(self) -> list[str]:
        """Tên các ứng viên Mimi vừa đọc, để web hiện nút chọn."""
        return [p.name for p in self.pending.candidates] if self.pending else []


class TaskManager:
    """Danh sách task + chạy từng task. broadcast(msg) gửi tới mọi tab; notify(task, text) đọc thông báo.
    on_label(record): mỗi lần người dùng chỉ ra dự án đúng (trả lời khi Mimi hỏi lại, "sai rồi", nút sửa) thì
    ghi một nhãn hiểu ý, nguồn dữ liệu cho vòng tự học (Phase 8 D)."""

    def __init__(self, registry: ProjectRegistry, launcher: VsCodeLauncher, *,
                 broadcast: Callable[[dict], Awaitable[None]],
                 notify: Callable[[Task, str], Awaitable[None]],
                 claude_dir: Path | None = None, log_dir: Path | None = None, stall_s: float = 12,
                 submit_timeout_s: float = 600, task_timeout_s: float = 7200,
                 watcher_factory: Callable[..., SessionWatcher] = SessionWatcher,
                 on_label: Callable[[dict], None] | None = None):
        self.registry = registry
        self.launcher = launcher
        self.broadcast = broadcast
        self.notify = notify
        self.claude_dir = claude_dir or Path.home() / ".claude" / "projects"
        self.log = AskLogger(log_dir) if log_dir else None
        self.stall_s, self.submit_timeout_s, self.task_timeout_s = stall_s, submit_timeout_s, task_timeout_s
        self.watcher_factory = watcher_factory
        self.on_label = on_label
        self.tasks: dict[int, Task] = {}
        self.next_id = 0
        self.refreshed_at = time.monotonic()

    def _fresh(self) -> ProjectRegistry:
        if time.monotonic() - self.refreshed_at > REFRESH_S:
            self.registry.refresh()
            self.refreshed_at = time.monotonic()
        return self.registry

    def projects(self, refresh: bool = False) -> list[dict]:
        if refresh:
            self.refreshed_at = 0
        return [p.to_dict() for p in self._fresh().all()]

    def parse(self, text: str) -> DevTask | None:
        return parse_dev_task(text, self._fresh())

    def snapshot(self) -> list[dict]:
        return [t.to_dict() for t in self.tasks.values()]

    def stt_names(self, limit: int = STT_NAMES) -> list[str]:
        """Tên dự án dạng đọc lên ("job-hay" → "job hay") cho prompt STT ở tab Công việc, kèm biệt danh.
        Lấy `limit` dự án dùng gần nhất; dự án gần nhất để cuối (Whisper cắt đầu prompt khi quá dài)."""
        names: list[str] = []
        for p in reversed(self._fresh().all()[:limit]):
            for n in (spoken_name(p), *p.aliases):
                if n and n not in names:
                    names.append(n)
        return names

    def latest(self) -> Task | None:
        """Task đang chạy mới nhất, không có thì task mới nhất."""
        running = [t for t in self.tasks.values() if not t.finished]
        pool = running or list(self.tasks.values())
        return max(pool, key=lambda t: t.task_id) if pool else None

    def last_project(self) -> Project | None:
        with_project = [t for t in self.tasks.values() if t.project is not None]
        return max(with_project, key=lambda t: t.task_id).project if with_project else None

    def route(self, text: str, *, work: bool = False, focus: str | None = None,
              pending: Pending | None = None, turn_id: str | None = None) -> Route | None:
        """Câu nói/gõ hỏi Mimi → Route, hoặc None để hỏi Claude như thường.

        work: chế độ công việc (tab Công việc đang mở): mọi câu đều là việc; câu không nêu tên dự án thì làm ở
        focus (tên dự án, None = dự án của việc gần nhất). pending: Mimi vừa hỏi "dự án nào?", câu này mà là
        tên dự án / "ừ" / "cái thứ hai" thì giao yêu cầu đang chờ. turn_id: lượt nhận dạng của câu này.
        """
        query = match_work_query(text)
        if query and (work or any(not t.finished for t in self.tasks.values())):
            if query == "status":
                return Route(reply=status_text(self.latest()))
            task = self.latest()
            if task is not None and self.cancel(task.task_id):
                return Route(reply=f"Mình thôi theo dõi việc ở dự án {spoken_name(task.project)}. "
                                   "Claude trong VS Code vẫn làm tiếp.")
            return Route(reply="Không có việc nào đang theo dõi.")
        reg = self._fresh()
        kind, rest = reply_kind(text)
        if pending is not None:
            if route := self._answer(text, kind, rest, pending, work):
                return route
        elif kind == "no" and (route := self._deny(rest, work)):
            return route
        elif kind is not None and not rest and work:   # "ừ" khi Mimi không hỏi gì: đừng đem đi giao việc
            return Route(reply="Bạn cần mình làm gì nhé?")
        dev = self._dev(text, work)
        if dev is not None:
            if dev.project is None and dev.candidates:
                return Route(reply=ask_project_text(dev.candidates),
                             pending=Pending(dev.request, text, dev.project_query, dev.candidates[:3], turn_id=turn_id))
            return Route(dev=dev, turn_id=turn_id)
        if not work:
            return None
        opened = guess_open(text, reg)
        if opened is not None:   # "mở dạng karaoke": muốn mở dự án khác nhưng nghe không ra tên, hỏi lại
            return self._ask_open(opened, Pending("", text, turn_id=turn_id))
        project = (reg.get(focus) if focus else None) or self.last_project()
        if project is None:
            return Route(reply="Việc này làm ở dự án nào?", pending=Pending(text, text, turn_id=turn_id))
        return Route(dev=DevTask(project, [project], project.name, text, text), turn_id=turn_id)

    def _dev(self, text: str, work: bool) -> DevTask | None:
        dev = self.parse(text)
        if dev is None and work:   # tab Công việc: tên dự án ở đâu trong câu cũng nhận, kể cả khi thiếu "dự án"
            dev = find_project_anywhere(text, self._fresh())
        return dev

    def _ask_open(self, opened: DevTask, pending: Pending) -> Route:
        """Hỏi lại khi nghe "mở X" mà X không khớp chắc dự án nào."""
        pending = pending.again(query=opened.project_query, candidates=opened.candidates[:3])
        if opened.candidates:
            return Route(reply=ask_project_text(opened.candidates), pending=pending)
        return Route(reply=f"Mình không tìm thấy dự án {opened.project_query}. Bạn nói lại tên dự án nhé.",
                     pending=pending)

    def _answer(self, text: str, kind: str | int | None, rest: str, pending: Pending, work: bool) -> Route | None:
        """Câu trả lời khi Mimi đang hỏi lại tên dự án. None = không phải câu trả lời, xử lý như câu mới."""
        if kind is not None and not rest:
            if kind == "no":
                return Route(reply="Vậy là dự án nào? Bạn nói tên giúp mình nhé.", pending=pending.again(candidates=[]))
            i = 0 if kind == "yes" else kind
            if i < len(pending.candidates):
                return self._resolved(pending, pending.candidates[i], "confirm" if kind == "yes" else "choose", text)
            return Route(reply="Bạn nói tên dự án giúp mình nhé.", pending=pending.again())
        said = rest if kind is not None else text   # "không, job hay cơ" / "ừ, guitar karaoke"
        dev = self._dev(said, work)
        if dev is None:
            opened = guess_open(said, self._fresh())
            if opened is not None:   # "mở diễn": vẫn không nghe ra tên
                return self._ask_open(opened, pending)
            project, candidates = pick_project(self._fresh().match(said))   # chỉ nói tên: "job hay"
            if candidates:
                dev = DevTask(project, candidates, said, "", said)
            elif len(_WORD.findall(said)) <= 4:
                return Route(reply=f"Mình không tìm thấy dự án {said.strip(' .?!')}. Bạn nói lại tên nhé.",
                             pending=pending.again(candidates=[]))
            else:
                return None   # câu dài không có tên dự án: việc mới
        if dev.request:
            return None   # câu giao việc mới đầy đủ ("mở dự án X rồi ..."): không dính tới câu đang chờ
        if dev.project is None:
            return Route(reply=ask_project_text(dev.candidates),
                         pending=pending.again(query=dev.project_query, candidates=dev.candidates[:3]))
        return self._resolved(pending, dev.project, "name", text)

    def _resolved(self, pending: Pending, project: Project, via: str, answer: str) -> Route:
        """Người dùng đã chỉ ra dự án đúng cho câu đang chờ: ghi nhãn, giao việc."""
        self._label(heard=pending.heard, query=pending.query, project=project, wrong=pending.wrong, via=via,
                    request=pending.request, answer=answer, turn_id=pending.turn_id,
                    asked=[p.name for p in pending.candidates])
        dev = DevTask(project, [project], pending.query or project.name, pending.request, pending.heard or answer)
        return Route(dev=dev, turn_id=pending.turn_id)

    def _deny(self, rest: str, work: bool) -> Route | None:
        """"sai rồi" / "không phải, job hay cơ" ngay sau khi giao việc: thôi theo dõi việc vừa giao, hỏi lại
        hoặc giao sang dự án vừa nói. None nếu không có việc vừa giao (câu nói với Claude)."""
        recent = [t for t in self.tasks.values() if t.project is not None and time.time() - t.started_at < DENY_S]
        if not recent:
            return None
        task = max(recent, key=lambda t: t.task_id)
        pending = Pending(task.request, task.raw or task.request, task.query, wrong=task.project, turn_id=task.turn_id)
        if rest:
            dev = self._dev(rest, work)
            project = dev.project if dev else pick_project(self._fresh().match(rest))[0]
            if project is None or project == task.project:
                return None   # "sai rồi, EC2 không phải vậy": nói với Claude, hoặc việc mới
            self.cancel(task.task_id)
            return self._resolved(pending, project, "deny_name", rest)
        self.cancel(task.task_id)
        cands = [p for p in task.candidates if p != task.project]
        if not cands and task.query:
            cands = [p for p, _ in self._fresh().similar(task.query) if p != task.project]
        pending.candidates = cands[:3]
        head = f"Mình thôi theo dõi việc ở dự án {spoken_name(task.project)}. "
        if task.status in ("running", "waiting"):
            head += "Claude ở đó đã nhận yêu cầu, bạn dừng tay trong VS Code nhé. "
        if not cands:
            return Route(reply=head + "Vậy là dự án nào?", pending=pending)
        names = " hay ".join(spoken_name(p) for p in pending.candidates)
        return Route(reply=f"{head}Vậy là dự án {names}?", pending=pending)

    def fix(self, task_id: int, project: str, request: str | None = None, origin: object | None = None) -> Task | None:
        """Nút "Sai dự án" trên web: thôi theo dõi task cũ, ghi nhãn, giao lại ở dự án đúng."""
        old, found = self.tasks.get(task_id), self._fresh().get(project)
        if old is None or found is None:
            return None
        self.cancel(task_id)
        req = old.request if request is None else request.strip()
        self._label(heard=old.raw or old.request, query=old.query, project=found, wrong=old.project, via="fix",
                    request=req, answer="", turn_id=old.turn_id, asked=[p.name for p in old.candidates])
        dev = DevTask(found, [found], old.query, req, old.raw)
        return self.start(dev, old.source, origin, acked=False, turn_id=old.turn_id)

    def _label(self, *, project: Project, wrong: Project | None, **fields) -> None:
        if self.on_label is None:
            return
        try:
            self.on_label({"event": "intent", "project": project.name, "wrong": wrong and wrong.name, **fields})
        except Exception:
            log.exception("Không ghi được nhãn hiểu ý")

    def start(self, dev: DevTask, source: str, origin: object | None = None, acked: bool = True,
              run: bool = True, turn_id: str | None = None) -> Task:
        """Tạo task và chạy nền. Trả task ngay để người gọi đọc câu đáp (ack_text); acked=False nếu không đọc.
        run=False: người gọi tự gọi launch(task), để câu đáp đến web trước task_start."""
        self.next_id += 1
        task = Task(self.next_id, dev.project, dev.request.strip(), source, time.time(), query=dev.project_query,
                    candidates=dev.candidates, acked=acked, raw=dev.raw, turn_id=turn_id, origin=origin)
        self.tasks[task.task_id] = task
        for old in [t for t in self.tasks.values() if t.finished][: max(0, len(self.tasks) - MAX_TASKS)]:
            del self.tasks[old.task_id]
        if run:
            self.launch(task)
        return task

    def launch(self, task: Task) -> None:
        if task.runner is None:
            task.runner = asyncio.create_task(self._run(task))

    def start_text(self, text: str, project: str | None, source: str = "text", origin: object | None = None) -> Task:
        """Message task từ web: có tên dự án thì cả text là yêu cầu, không thì tách như câu nói."""
        if project:
            found = self._fresh().get(project)
            dev = DevTask(found, [], project, text, text)
        else:
            dev = self.parse(text) or DevTask(None, [], "", text, text)
        return self.start(dev, source, origin, acked=False)

    def cancel(self, task_id: int) -> bool:
        task = self.tasks.get(task_id)
        if task is None or task.finished or task.runner is None:
            return False
        task.runner.cancel()
        return True

    async def open_session(self, task_id: int) -> bool:
        task = self.tasks.get(task_id)
        if task is None or task.project is None:
            return False
        res = await self.launcher.open(task.project.path, session=task.session_id)
        return res.opened

    def close(self) -> None:
        for task in self.tasks.values():
            if task.runner and not task.runner.done():
                task.runner.cancel()

    async def _status(self, task: Task, status: str, detail: str | None = None) -> None:
        task.status, task.detail = status, detail
        await self.broadcast({"type": "task_status", "task_id": task.task_id, "status": status, "detail": detail})

    async def _end(self, task: Task, status: str, report: str) -> None:
        task.status, task.report, task.detail = status, report, None
        task.duration_ms = round((time.time() - task.started_at) * 1000)
        await self.broadcast({"type": "task_end", "task_id": task.task_id, "status": status, "report": report,
                              "files": task.files, "tools": task.tools, "duration_ms": task.duration_ms,
                              "session_id": task.session_id})
        if self.log:
            self.log.write(**task.to_dict())
        if task.acked and task.project is None:
            return  # "không tìm thấy dự án" đã nói trong câu đáp
        if text := spoken_report(task):
            await self.notify(task, text)

    async def _run(self, task: Task) -> None:
        await self.broadcast({"type": "task_start", "task_id": task.task_id, "project": task.project_dict(),
                              "request": task.request, "source": task.source, "started_at": task.started_at})
        try:
            if task.project is None:
                return await self._end(task, "failed", ack_text(task))
            async with asyncio.timeout(self.task_timeout_s):
                await self._drive(task)
        except asyncio.CancelledError:
            await self._end(task, "cancelled", "Đã ngừng theo dõi. Claude trong VS Code vẫn có thể đang làm tiếp.")
        except TimeoutError:
            await self._end(task, "failed", f"Quá {self.task_timeout_s / 3600:g} giờ chưa xong, Mimi thôi theo dõi.")
        except Exception as e:
            log.exception("Task %d lỗi", task.task_id)
            await self._end(task, "failed", f"Lỗi khi theo dõi: {e}")

    async def _drive(self, task: Task) -> None:
        project = task.project
        await self._status(task, "opening")
        since = time.time()
        res = await self.launcher.open(project.path, prompt=task.request or None)
        if not res.opened:
            return await self._end(task, "failed", res.note or "Không mở được VS Code.")
        if not task.request:
            return await self._end(task, "done", f"Đã mở dự án {project.name} trong VS Code.")
        if not res.prefilled:
            return await self._end(task, "failed", res.note or "Không đưa được yêu cầu vào ô chat Claude.")
        if not res.submitted:
            await self._status(task, "prefilled", res.note or "Yêu cầu đã điền sẵn, bấm Enter trong VS Code để gửi.")
            await self.notify(task, "Mình đã điền yêu cầu, bạn bấm Enter trong VS Code để gửi nhé.")

        watcher = self.watcher_factory(lambda: claude_project_dirs(project.path, self.claude_dir), task.request,
                                       since, stall_s=self.stall_s, submit_timeout_s=self.submit_timeout_s)
        step_ids: dict[str, Step] = {}
        told_waiting = False
        events = watcher.events()
        try:
            async for ev in events:
                if isinstance(ev, Submitted):
                    task.session_id = ev.session_id
                    await self._status(task, "running")
                elif isinstance(ev, ToolStep):
                    step = Step(len(task.steps) + 1, ev.tool, ev.summary)
                    step_ids[ev.tool_use_id] = step
                    task.steps.append(step)
                    task.tools += 1
                    await self.broadcast({"type": "task_step", "task_id": task.task_id, "step_id": step.step_id,
                                          "tool": step.tool, "summary": step.summary})
                elif isinstance(ev, ToolDone):
                    if step := step_ids.get(ev.tool_use_id):
                        step.ok = ev.ok
                        await self.broadcast({"type": "task_step_end", "task_id": task.task_id,
                                              "step_id": step.step_id, "ok": ev.ok})
                    if task.status == "waiting":
                        await self._status(task, "running")
                elif isinstance(ev, Waiting):
                    await self._status(task, "waiting", f"{ev.step.tool}: {ev.step.summary}")
                    if not told_waiting:
                        told_waiting = True
                        await self.notify(task, "Claude trong VS Code đang chờ bạn duyệt, hoặc đang chạy lệnh lâu.")
                elif isinstance(ev, Finished):
                    task.session_id = ev.session_id or task.session_id
                    task.files, task.tools = list(ev.files), max(task.tools, ev.tools)
                    return await self._end(task, "interrupted" if ev.interrupted else "done", ev.text.strip())
                elif isinstance(ev, NotSubmitted):
                    return await self._end(task, "failed", f"Không thấy yêu cầu được gửi trong VS Code sau "
                                                           f"{self.submit_timeout_s / 60:g} phút, Mimi thôi theo dõi.")
        finally:
            await events.aclose()
        await self._end(task, "failed", "Mất dấu phiên Claude trong VS Code.")
