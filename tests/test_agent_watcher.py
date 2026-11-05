"""Test giao việc cho VS Code (T-011): SessionWatcher đọc log jsonl giả ghi dần, VsCodeLauncher với backend giả.

Không mở VS Code thật, không gửi phím thật."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from server.agent import vscode as V
from server.agent.vscode import LaunchResult, VsCodeLauncher, claude_uri, title_matches
from server.agent.watcher import (Finished, NotSubmitted, SessionWatcher, Submitted, ToolDone, ToolStep, Waiting,
                                  project_log_dirs, project_slug, prompt_matches, summarize)

PROMPT = "Thêm phân trang cho trang danh sách job, mỗi trang 20 dòng"


# ---------------------------------------------------------------------------------------------------------
# Dựng bản ghi log giống Claude Code ghi


def ts(offset: float = 0.0) -> str:
    return datetime.fromtimestamp(time.time() + offset, timezone.utc).isoformat().replace("+00:00", "Z")


def user_text(text, uuid="u-prompt", offset=0.0, **kw) -> dict:
    return {"type": "user", "isSidechain": False, "timestamp": ts(offset), "sessionId": "S1", "uuid": uuid,
            "origin": {"kind": "human"}, "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            **kw}


def asst(mid: str, block: dict, stop=None, sidechain=False) -> dict:
    return {"type": "assistant", "isSidechain": sidechain, "requestId": "req_" + mid, "timestamp": ts(),
            "message": {"id": mid, "role": "assistant", "stop_reason": stop, "content": [block]}}


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def tool_use(tid: str, name: str, **inp) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def result(tid: str, err=False, sidechain=False) -> dict:
    return {"type": "user", "isSidechain": sidechain, "timestamp": ts(), "toolUseResult": {},
            "permissionDecision": {"decision": "accept", "source": "config"},
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": "x",
                                                     "is_error": err}]}}


def line(rec: dict) -> str:
    return json.dumps(rec, ensure_ascii=False) + "\n"


async def append(path: Path, *chunks: str | float):
    """Ghi dần vào file: chuỗi thì ghi thêm, số thì ngủ chừng đó giây."""
    for c in chunks:
        if isinstance(c, (int, float)):
            await asyncio.sleep(c)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="") as f:
                f.write(c)


async def collect(watcher: SessionWatcher, writer=None, timeout: float = 5.0) -> list:
    task = asyncio.create_task(writer) if writer else None
    out = []
    try:
        async with asyncio.timeout(timeout):
            async for ev in watcher.events():
                out.append(ev)
    finally:
        if task:
            await task
    return out


def watcher(dirs, prompt=PROMPT, **kw) -> SessionWatcher:
    opts = dict(poll_s=0.01, stall_s=1.0, settle_s=0.4, submit_timeout_s=3.0)
    opts.update(kw)
    return SessionWatcher(dirs, prompt, time.time(), **opts)


def kinds(events) -> list[str]:
    return [type(e).__name__ for e in events]


# ---------------------------------------------------------------------------------------------------------
# SessionWatcher


def test_full_turn_with_partial_lines_and_split_end_turn(tmp_path):
    f = tmp_path / "S1.jsonl"
    # Lượt cũ trong cùng file (trước prompt) phải bị bỏ qua
    f.write_text(line(user_text("câu hỏi cũ", uuid="u-old", offset=-3600))
                 + line(asst("m0", text("trả lời cũ"), "end_turn")), encoding="utf-8")
    edit = line(asst("m2", tool_use("t2", "Edit", file_path="src/list.py", old_string="a", new_string="b"),
                     "tool_use"))

    async def write():
        await append(f, 0.05, line(user_text(PROMPT)), line({"type": "attachment", "x": 1}),
                     line(user_text("nội dung skill", uuid="u-meta", isMeta=True)),
                     line(asst("m1", {"type": "thinking", "thinking": ""}, "tool_use")),
                     line(asst("m1", tool_use("t1", "Bash", command="npm test"), "tool_use")), 0.05,
                     edit[:30], 0.08, edit[30:],  # dòng ghi dở dang
                     line(result("t1")), line(result("t2")),
                     line(asst("m3", tool_use("t3", "Write", file_path="src/pager.py", content="..."), "tool_use")),
                     line(result("t3", err=True)),
                     line(asst("m4", tool_use("t4", "Edit", file_path="src/list.py"), "tool_use")),
                     line(result("t4")),
                     line(asst("m5", {"type": "thinking", "thinking": ""}, "end_turn")), 0.05,
                     line(asst("m5", text("Đã thêm phân trang."), "end_turn")), 0.05,
                     line(asst("m5", text("Chạy test đều qua."), "end_turn")),
                     line({"type": "last-prompt"}), line({"type": "ai-title", "title": "x"}))

    ev = asyncio.run(collect(watcher(lambda: [tmp_path]), write()))
    assert kinds(ev) == ["Submitted", "ToolStep", "ToolStep", "ToolDone", "ToolDone", "ToolStep", "ToolDone",
                         "ToolStep", "ToolDone", "Finished"]
    assert ev[0] == Submitted("S1", f)
    assert ev[1] == ToolStep("t1", "Bash", "npm test")
    assert ev[2] == ToolStep("t2", "Edit", "src/list.py")
    assert ev[6] == ToolDone("t3", False)
    assert ev[-1] == Finished("S1", "Đã thêm phân trang.\n\nChạy test đều qua.", 4, ["src/list.py", "src/pager.py"])


def test_end_turn_finishes_on_next_message_without_waiting_settle(tmp_path):
    f = tmp_path / "S1.jsonl"

    async def write():
        await append(f, line(user_text(PROMPT)), line(asst("m1", text("Xong."), "end_turn")), 0.1,
                     line(user_text("câu tiếp theo", uuid="u2")))

    t0 = time.monotonic()
    ev = asyncio.run(collect(watcher(lambda: [tmp_path], settle_s=30), write()))
    assert ev[-1] == Finished("S1", "Xong.", 0, [])
    assert time.monotonic() - t0 < 3


def test_stalled_tool_reports_waiting_once(tmp_path):
    f = tmp_path / "S1.jsonl"

    async def write():
        await append(f, line(user_text(PROMPT)),
                     line(asst("m1", tool_use("t1", "Bash", command="rm -rf build"), "tool_use")), 1.0,
                     line(result("t1")), line(asst("m2", text("Đã xoá."), "end_turn")))

    ev = asyncio.run(collect(watcher(lambda: [tmp_path], stall_s=0.2), write()))
    assert kinds(ev) == ["Submitted", "ToolStep", "Waiting", "ToolDone", "Finished"]
    assert ev[2] == Waiting(ToolStep("t1", "Bash", "rm -rf build"))


def test_interrupted_turn(tmp_path):
    f = tmp_path / "S1.jsonl"
    stop = {"type": "user", "isSidechain": False, "timestamp": ts(),
            "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]}}

    async def write():
        await append(f, line(user_text(PROMPT)), line(asst("m1", text("Tôi sẽ sửa list.py."), "tool_use")),
                     line(asst("m1", tool_use("t1", "Edit", file_path="list.py"), "tool_use")), 0.05,
                     line(result("t1", err=True)), line(stop))

    ev = asyncio.run(collect(watcher(lambda: [tmp_path], settle_s=30), write()))
    assert ev[-1] == Finished("S1", "Tôi sẽ sửa list.py.", 1, ["list.py"], interrupted=True)
    assert ToolDone("t1", False) in ev


def test_sidechain_records_are_ignored(tmp_path):
    f = tmp_path / "S1.jsonl"

    async def write():
        await append(f, line(user_text(PROMPT)),
                     line(asst("m1", tool_use("t1", "Task", description="Tìm file router"), "tool_use")),
                     line(asst("s1", tool_use("st1", "Grep", pattern="router"), "tool_use", sidechain=True)),
                     line(result("st1", sidechain=True)),
                     line(asst("s2", text("kết quả của subagent"), "end_turn", sidechain=True)), 0.4,
                     line(result("t1")), line(asst("m2", text("Router nằm ở app.py."), "end_turn")))

    ev = asyncio.run(collect(watcher(lambda: [tmp_path], stall_s=30), write()))
    assert kinds(ev) == ["Submitted", "ToolStep", "ToolDone", "Finished"]
    assert ev[1].summary == "Tìm file router"
    assert ev[-1] == Finished("S1", "Router nằm ở app.py.", 1, [])


def test_edited_prompt_still_matches(tmp_path):
    f = tmp_path / "S1.jsonl"
    edited = "  thêm phân trang cho trang danh sách job,\n mỗi trang 25 dòng nhé"

    async def write():
        await append(f, 0.05, line(user_text(edited)), line(asst("m1", text("Ok."), "end_turn")))

    ev = asyncio.run(collect(watcher(lambda: [tmp_path]), write()))
    assert kinds(ev) == ["Submitted", "Finished"]


def test_prompt_matching_rules():
    assert prompt_matches("Mở  dự án\njob hay", "mở dự án job hay")
    assert prompt_matches(PROMPT + " và thêm nút sang trang cuối cùng nữa", PROMPT)  # chung 40 ký tự đầu
    assert prompt_matches("Thêm phân trang cho trang job", "Thêm phân trang cho trang jobs")  # ratio
    assert not prompt_matches("Viết test cho module thanh toán", PROMPT)
    assert not prompt_matches("", PROMPT)


def test_not_submitted_with_other_prompt_and_old_record(tmp_path):
    f = tmp_path / "S1.jsonl"
    f.write_text(line(user_text(PROMPT, offset=-600))  # đúng prompt nhưng quá cũ
                 + line(user_text("Giải thích dependency injection", uuid="u2")), encoding="utf-8")
    ev = asyncio.run(collect(watcher(lambda: [tmp_path], submit_timeout_s=0.3)))
    assert ev == [NotSubmitted()]


def test_not_submitted_with_fake_clock(tmp_path):
    ticks = iter(range(0, 10_000, 100))
    w = SessionWatcher(lambda: [tmp_path / "chưa-có"], PROMPT, time.time(), poll_s=0, submit_timeout_s=600,
                       clock=lambda: float(next(ticks)))
    assert asyncio.run(collect(w, timeout=2)) == [NotSubmitted()]


def test_log_dir_appears_late(tmp_path):
    root = tmp_path / "projects"
    project = Path("D:/work/job hay")

    async def write():
        await append(root / "d--work-job-hay" / "S1.jsonl", 0.15, line(user_text(PROMPT)),
                     line(asst("m1", text("Đã xong."), "end_turn")))

    ev = asyncio.run(collect(watcher(lambda: project_log_dirs(project, root)), write()))
    assert kinds(ev) == ["Submitted", "Finished"]
    assert ev[0].file.parent.name == "d--work-job-hay"


def test_project_log_dirs_ignores_drive_letter_case(tmp_path):
    (tmp_path / "d--projects-job-hay").mkdir()
    (tmp_path / "d--projects-job-hay-2").mkdir()
    assert project_slug(r"d:\projects\job_hay") == "d--projects-job-hay"
    got = project_log_dirs(r"D:\projects\job.hay", tmp_path)
    assert [d.name for d in got] == ["d--projects-job-hay"]
    assert project_log_dirs("x", tmp_path / "không-có") == []


def test_summarize():
    assert summarize("mcp__claude_ai_Claude_Docs__batch", {"x": 1}) == "claude_ai_Claude_Docs: batch"
    assert summarize("Grep", {"pattern": "def \\w+", "path": "src"}) == "def \\w+"
    assert summarize("WebFetch", {"url": "https://a.b", "prompt": "tóm tắt"}) == "https://a.b"
    long = summarize("Bash", {"command": "echo " + "x" * 300})
    assert len(long) == 120 and long.endswith("…")
    assert summarize("Bash", {"command": "a\n   b"}) == "a b"
    assert summarize("TodoWrite", {"todos": []}) == ""


# ---------------------------------------------------------------------------------------------------------
# VsCodeLauncher với backend giả


class FakeBackend:
    def __init__(self, windows=(), appear: tuple[int, str] | None = None, appear_after=2, fg=1,
                 focus_ok=True, steal_after_uri=False, fail=None):
        self.windows = list(windows)
        self.appear, self.appear_after = appear, appear_after
        self.fg = fg
        self.focus_ok, self.steal_after_uri, self.fail = focus_ok, steal_after_uri, fail
        self.calls: list[tuple] = []
        self._lists_after_run: int | None = None

    def run_code(self, exe, path):
        if self.fail == "run":
            raise OSError("không chạy được")
        self.calls.append(("run", exe, Path(path)))
        self._lists_after_run = 0

    def list_windows(self):
        if self.fail == "list":
            raise RuntimeError("EnumWindows hỏng")
        if self._lists_after_run is not None:
            self._lists_after_run += 1
            if self.appear and self._lists_after_run >= self.appear_after and self.appear not in self.windows:
                self.windows.append(self.appear)
        return list(self.windows)

    def foreground(self):
        return self.fg

    def focus(self, hwnd):
        self.calls.append(("focus", hwnd))
        if self.focus_ok:
            self.fg = hwnd
        return self.focus_ok

    def open_uri(self, uri):
        self.calls.append(("uri", uri))
        if self.steal_after_uri:
            self.fg = 999

    def send_enter(self):
        self.calls.append(("enter",))


OTHER = (1, "app.py - AI-assistent - Visual Studio Code")
JOB = (42, "● README.md - Job-Hay - Visual Studio Code - Modified")


def launcher(backend, **kw) -> VsCodeLauncher:
    opts = dict(exe="code", window_wait_s=1.0, ready_wait_s=0.0, submit_delay_s=0.0)
    opts.update(kw)
    return VsCodeLauncher(backend=backend, **opts)


def ops(b: FakeBackend) -> list[str]:
    return [c[0] for c in b.calls]


def test_launch_new_window_waits_ready_then_submits():
    b = FakeBackend([OTHER], appear=JOB)
    t0 = time.monotonic()
    r = asyncio.run(launcher(b, ready_wait_s=0.3).open(Path("D:/work/job-hay"), PROMPT))
    assert r == LaunchResult(True, True, True)
    assert time.monotonic() - t0 >= 0.3
    assert b.calls == [("run", "code", Path("D:/work/job-hay")), ("focus", 42), ("uri", claude_uri(PROMPT)),
                       ("enter",)]


def test_launch_existing_window_skips_ready_wait():
    b = FakeBackend([OTHER, JOB])
    t0 = time.monotonic()
    r = asyncio.run(launcher(b, ready_wait_s=10).open(Path("D:/work/JOB-HAY"), PROMPT))
    assert r.submitted and time.monotonic() - t0 < 3
    assert ("focus", 42) in b.calls


def test_launch_focus_fails_does_not_open_uri_or_press_enter():
    b = FakeBackend([OTHER, JOB], focus_ok=False)
    r = asyncio.run(launcher(b).open(Path("D:/work/job-hay"), PROMPT))
    assert (r.opened, r.prefilled, r.submitted) == (True, False, False) and r.note
    assert "uri" not in ops(b) and "enter" not in ops(b)


def test_launch_foreground_changes_before_enter():
    b = FakeBackend([OTHER, JOB], steal_after_uri=True)
    r = asyncio.run(launcher(b).open(Path("D:/work/job-hay"), PROMPT))
    assert (r.opened, r.prefilled, r.submitted) == (True, True, False)
    assert "Enter" in r.note and "enter" not in ops(b)


def test_launch_without_auto_submit():
    b = FakeBackend([OTHER, JOB])
    r = asyncio.run(launcher(b, auto_submit=False).open(Path("D:/work/job-hay"), PROMPT))
    assert (r.prefilled, r.submitted) == (True, False) and "Enter" in r.note
    assert "enter" not in ops(b)


def test_launch_folder_only():
    b = FakeBackend([OTHER, JOB])
    r = asyncio.run(launcher(b).open(Path("D:/work/job-hay")))
    assert r == LaunchResult(True, False, False)
    assert ops(b) == ["run", "focus"]


def test_launch_resume_session_without_prompt():
    b = FakeBackend([OTHER, JOB])
    r = asyncio.run(launcher(b).open(Path("D:/work/job-hay"), session="abc-123"))
    assert (r.prefilled, r.submitted) == (True, False)
    assert ("uri", claude_uri(session="abc-123")) in b.calls and "enter" not in ops(b)


def test_launch_window_never_appears():
    b = FakeBackend([OTHER, (7, "job-hay-2 - Visual Studio Code"), (8, "job-hay - Notepad")])
    r = asyncio.run(launcher(b, window_wait_s=0.3).open(Path("D:/work/job-hay"), PROMPT))
    assert (r.opened, r.prefilled, r.submitted) == (False, False, False) and "job-hay" in r.note


@pytest.mark.parametrize("fail", ["run", "list"])
def test_launch_never_raises_on_backend_errors(fail):
    r = asyncio.run(launcher(FakeBackend([JOB], fail=fail)).open(Path("D:/work/job-hay"), PROMPT))
    assert not r.opened and r.note


def test_launch_without_code_in_path(monkeypatch):
    monkeypatch.setattr(V.shutil, "which", lambda name: None)
    r = asyncio.run(VsCodeLauncher(backend=FakeBackend([JOB])).open(Path("D:/work/job-hay"), PROMPT))
    assert not r.opened and "code" in r.note


def test_claude_uri_encoding():
    prompt = "Mở dự án job hay & thêm phân trang #12\nrồi chạy test + build 100%"
    uri = claude_uri(prompt, "s-1")
    assert uri.startswith("vscode://anthropic.claude-code/open?prompt=")
    query = uri.split("?", 1)[1]
    for bad in (" ", "\n", "#", "+"):
        assert bad not in query
    assert query.count("&") == 1 and "%20" in query
    parts = urlsplit(uri)
    assert parts.fragment == ""
    assert parse_qs(parts.query) == {"prompt": [prompt], "session": ["s-1"]}
    assert claude_uri() == "vscode://anthropic.claude-code/open"
    assert claude_uri(session="abc") == "vscode://anthropic.claude-code/open?session=abc"


def test_title_matches():
    assert title_matches("job-hay - Visual Studio Code", "Job-Hay")
    assert title_matches("● page.tsx (Working Tree) — job-hay — Visual Studio Code", "job-hay")
    assert title_matches("job-hay (Workspace) - Visual Studio Code", "job-hay")
    assert title_matches("a.py - my - app - Visual Studio Code", "my - app")
    assert not title_matches("job-hay - Visual Studio Code", "job")
    assert not title_matches("job-hay - Notepad", "job-hay")
    assert not title_matches("jobhay - Visual Studio Code", "job-hay")
