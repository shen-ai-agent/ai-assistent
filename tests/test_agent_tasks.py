"""Test giao việc cho VS Code (T-011) qua WebSocket: launcher và watcher giả, không mở VS Code thật."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.agent.projects import ProjectRegistry
from server.agent.tasks import Task, first_sentences, spoken_report
from server.agent.vscode import LaunchResult
from server.agent.watcher import Finished, NotSubmitted, Submitted, ToolDone, ToolStep, Waiting
from server.app import create_app
from server.config import Config
from tests.test_server import FakeBridge, FakeEngine, pcm, recv_until


class FakeLauncher:
    def __init__(self, result: LaunchResult | None = None):
        self.result = result or LaunchResult(opened=True, prefilled=True, submitted=True)
        self.calls: list[tuple] = []

    async def open(self, path: Path, prompt: str | None = None, session: str | None = None) -> LaunchResult:
        self.calls.append((path, prompt, session))
        return self.result


class FakeWatcher:
    """Phát lần lượt các sự kiện; block=True thì treo sau sự kiện cuối (để test ngừng theo dõi)."""

    def __init__(self, events: list, block: bool = False):
        self.evs, self.block = events, block
        self.args = None

    def __call__(self, dirs, prompt, since, **kw):
        self.args = (dirs, prompt, since, kw)
        return self

    async def events(self):
        for ev in self.evs:
            await asyncio.sleep(0)
            yield ev
        if self.block:
            await asyncio.Event().wait()


def make_projects(root: Path) -> Path:
    for name in ("job-hay", "job-hay-old", "fruit-rush", "guitar-karaoke"):
        (root / name).mkdir(parents=True)
        (root / name / "README.md").write_text(f"# {name}\n\nDự án {name}.\n", encoding="utf-8")
    return root


@pytest.fixture
def agent_app(tmp_path):
    root = make_projects(tmp_path / "projects")
    launcher = FakeLauncher()
    registry = ProjectRegistry([root], None, tmp_path / "claude")
    bridge = FakeBridge(["Dự án là một tập hợp công việc."])
    cfg = Config(log_dir=tmp_path / "logs", min_speech_ms=0, agent_enabled=True, agent_roots=[root],
                 agent_claude_dir=tmp_path / "claude", agent_log_dir=tmp_path / "tasks",
                 bridge_log_dir=tmp_path / "sessions")
    engine = FakeEngine()
    app = create_app(cfg, engine_factory=lambda _cfg: engine, bridge_factory=lambda _cfg: bridge,
                     agent_factory=lambda _cfg: (registry, launcher))
    with TestClient(app) as client:
        client.engine = engine
        yield client, launcher, bridge, root


def set_watcher(client, watcher: FakeWatcher):
    client.app.state.agent.watcher_factory = watcher


def test_hello_has_agent_and_tasks(agent_app):
    client, *_ = agent_app
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
        assert hello["agent"] == {"auto_submit": True}
        assert recv_until(ws, "tasks")["items"] == []
        ws.send_json({"type": "projects"})
        names = [p["name"] for p in recv_until(ws, "projects")["items"]]
        assert set(names) >= {"job-hay", "fruit-rush"}


def test_spoken_task_runs_to_report(agent_app):
    client, launcher, bridge, root = agent_app
    set_watcher(client, FakeWatcher([
        Submitted("sess-1", root / "x.jsonl"),
        ToolStep("t1", "Edit", "src/list.tsx"),
        Waiting(ToolStep("t1", "Edit", "src/list.tsx")),
        ToolDone("t1", True),
        Finished("sess-1", "Đã thêm phân trang. Trang danh sách hiện 20 job mỗi trang.", 1, ["src/list.tsx"]),
    ]))
    with client.websocket_connect("/ws") as ws:
        recv_until(ws, "tasks")
        ws.send_json({"type": "ask", "text": "mở dự án job hay rồi thêm phân trang cho trang danh sách"})
        cmd = recv_until(ws, "command")
        assert cmd["name"] == "dev_task" and "job hay" in cmd["reply"]
        start = recv_until(ws, "task_start")
        assert start["project"]["name"] == "job-hay"
        assert start["request"] == "thêm phân trang cho trang danh sách"
        assert recv_until(ws, "task_status")["status"] == "opening"
        assert recv_until(ws, "task_status")["status"] == "running"
        step = recv_until(ws, "task_step")
        assert (step["tool"], step["summary"]) == ("Edit", "src/list.tsx")
        assert recv_until(ws, "task_status")["status"] == "waiting"
        assert recv_until(ws, "task_step_end")["ok"] is True
        end = recv_until(ws, "task_end")
        assert end["status"] == "done" and end["session_id"] == "sess-1"
        assert end["files"] == ["src/list.tsx"] and "phân trang" in end["report"]
    assert launcher.calls == [(root / "job-hay", "thêm phân trang cho trang danh sách", None)]
    assert bridge.questions == []   # không hỏi Claude của Mimi


def test_question_about_projects_goes_to_claude(agent_app):
    client, launcher, bridge, _ = agent_app
    with client.websocket_connect("/ws") as ws:
        recv_until(ws, "tasks")
        ws.send_json({"type": "ask", "text": "dự án là gì"})
        recv_until(ws, "answer_end")
    assert bridge.questions == ["dự án là gì"] and launcher.calls == []


def test_unknown_project_fails_without_launching(agent_app):
    client, launcher, *_ = agent_app
    with client.websocket_connect("/ws") as ws:
        recv_until(ws, "tasks")
        ws.send_json({"type": "ask", "text": "mở dự án quantum zebra rồi sửa lỗi"})
        cmd = recv_until(ws, "command")
        assert cmd["name"] == "dev_task" and "không tìm thấy" in cmd["reply"].lower()
        end = recv_until(ws, "task_end")
        assert end["status"] == "failed"
    assert launcher.calls == []


def test_task_message_prefilled_then_not_submitted(agent_app):
    client, launcher, _, root = agent_app
    launcher.result = LaunchResult(True, True, False, "Đã điền sẵn yêu cầu, bấm Enter trong VS Code để gửi.")
    set_watcher(client, FakeWatcher([NotSubmitted()]))
    with client.websocket_connect("/ws") as ws:
        recv_until(ws, "tasks")
        ws.send_json({"type": "task", "text": "viết test cho game loop", "project": "fruit-rush"})
        assert recv_until(ws, "task_start")["request"] == "viết test cho game loop"
        recv_until(ws, "task_status")   # opening
        st = recv_until(ws, "task_status")
        assert st["status"] == "prefilled" and "Enter" in st["detail"]
        assert recv_until(ws, "task_end")["status"] == "failed"
    assert launcher.calls == [(root / "fruit-rush", "viết test cho game loop", None)]


def test_open_only_and_snapshot_after_reconnect(agent_app):
    client, launcher, _, root = agent_app
    with client.websocket_connect("/ws") as ws:
        recv_until(ws, "tasks")
        ws.send_json({"type": "task", "text": "", "project": "job-hay"})
        end = recv_until(ws, "task_end")
        assert end["status"] == "done"
    assert launcher.calls == [(root / "job-hay", None, None)]
    with client.websocket_connect("/ws") as ws:
        items = recv_until(ws, "tasks")["items"]
        assert [(t["project"]["name"], t["status"]) for t in items] == [("job-hay", "done")]


def test_cancel_stops_watching(agent_app):
    client, _, _, root = agent_app
    set_watcher(client, FakeWatcher([Submitted("s2", root / "y.jsonl")], block=True))
    with client.websocket_connect("/ws") as ws:
        recv_until(ws, "tasks")
        ws.send_json({"type": "task", "text": "refactor", "project": "job-hay"})
        task_id = recv_until(ws, "task_start")["task_id"]
        while recv_until(ws, "task_status")["status"] != "running":
            pass
        ws.send_json({"type": "task_cancel", "task_id": task_id})
        end = recv_until(ws, "task_end")
        assert end["status"] == "cancelled" and end["session_id"] == "s2"
        ws.send_json({"type": "task_open", "task_id": task_id})
    assert client.app.state.agent.launcher.calls[-1] == (root / "job-hay", None, "s2")


def test_agent_disabled_by_default(tmp_path):
    cfg = Config(log_dir=tmp_path / "logs")
    with TestClient(create_app(cfg, engine_factory=lambda _cfg: FakeEngine())) as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["agent"] is None
            ws.send_json({"type": "projects"})
            assert recv_until(ws, "error")["code"] == "agent_disabled"


def test_spoken_report_and_first_sentences():
    text = "Đã thêm phân trang. Mỗi trang 20 job.\n\n```ts\nconst x = 1\n```\n- Sửa API.\n- Thêm test."
    assert first_sentences(text, 40) == "Đã thêm phân trang. Mỗi trang 20 job."
    from server.agent.projects import Project
    t = Task(1, Project("job-hay", Path("D:/projects/job-hay")), "thêm phân trang", "voice", 0.0,
             status="done", report=text, files=["a.ts", "b.ts"])
    spoken = spoken_report(t)
    assert spoken.startswith("Xong việc ở dự án job hay. Claude đã sửa 2 file.") and "```" not in spoken
    t.status = "cancelled"
    assert spoken_report(t) is None


# ---------- Chế độ công việc (tab Công việc) và câu hỏi tiến độ ----------

def test_work_mode_uses_focus_project(agent_app):
    client, launcher, bridge, root = agent_app
    set_watcher(client, FakeWatcher([NotSubmitted()]))
    with client.websocket_connect("/ws") as ws:
        recv_until(ws, "tasks")
        ws.send_json({"type": "work", "on": True, "project": "fruit-rush"})
        ws.send_json({"type": "ask", "text": "viết test cho game loop"})
        cmd = recv_until(ws, "command")
        assert cmd["name"] == "dev_task" and "fruit rush" in cmd["reply"]
        recv_until(ws, "task_end")
    assert launcher.calls == [(root / "fruit-rush", "viết test cho game loop", None)]
    assert bridge.questions == []


def test_work_mode_asks_project_then_uses_answer(agent_app):
    client, launcher, bridge, root = agent_app
    set_watcher(client, FakeWatcher([NotSubmitted()]))
    with client.websocket_connect("/ws") as ws:
        recv_until(ws, "tasks")
        ws.send_json({"type": "work", "on": True, "project": None})
        ws.send_json({"type": "ask", "text": "thêm phân trang"})
        cmd = recv_until(ws, "command")
        assert cmd["name"] == "work" and "dự án nào" in cmd["reply"]
        ws.send_json({"type": "ask", "text": "job hay"})
        assert recv_until(ws, "command")["name"] == "dev_task"
        assert recv_until(ws, "task_start")["request"] == "thêm phân trang"
        recv_until(ws, "task_end")
        # Việc sau không nêu dự án: làm ở dự án của việc gần nhất
        ws.send_json({"type": "ask", "text": "viết thêm test"})
        assert recv_until(ws, "task_start")["project"]["name"] == "job-hay"
        recv_until(ws, "task_end")
    assert [c[:2] for c in launcher.calls] == [(root / "job-hay", "thêm phân trang"), (root / "job-hay", "viết thêm test")]
    assert bridge.questions == []


def test_work_mode_open_unknown_name_asks_back(agent_app):
    # "mở X" mà X không khớp chắc dự án nào: hỏi lại kèm ứng viên, không đem câu giao cho dự án đang chọn
    client, launcher, bridge, root = agent_app
    set_watcher(client, FakeWatcher([NotSubmitted()]))
    with client.websocket_connect("/ws") as ws:
        recv_until(ws, "tasks")
        ws.send_json({"type": "work", "on": True, "project": "job-hay"})
        ws.send_json({"type": "ask", "text": "mở dạng karaoke."})
        cmd = recv_until(ws, "command")
        assert cmd["name"] == "work" and "guitar karaoke" in cmd["reply"]
        ws.send_json({"type": "ask", "text": "mở diễn."})
        cmd = recv_until(ws, "command")
        assert cmd["name"] == "work" and "không tìm thấy dự án diễn" in cmd["reply"]
        ws.send_json({"type": "ask", "text": "guitar karaoke"})
        assert recv_until(ws, "command")["name"] == "dev_task"
        recv_until(ws, "task_end")
    assert launcher.calls == [(root / "guitar-karaoke", None, None)]   # chỉ mở dự án
    assert bridge.questions == []


def test_guess_open(agent_app):
    from server.agent.intent import guess_open
    client, *_ = agent_app
    reg = client.app.state.agent.registry

    def guess(text):
        dev = guess_open(text, reg)
        return dev and [p.name for p in dev.candidates]

    assert guess("mở dạng karaoke") == ["guitar-karaoke"]
    assert guess("Mimi ơi, giúp mình mở diễn nhé") == []
    assert guess("mở file README rồi sửa lỗi chính tả") is None   # có động từ việc: là việc, không phải mở dự án
    assert guess("thêm phân trang") is None
    client, launcher, _, root = agent_app
    set_watcher(client, FakeWatcher([NotSubmitted()]))
    with client.websocket_connect("/ws") as ws:
        recv_until(ws, "tasks")
        ws.send_json({"type": "ask", "text": "mở dự án job rồi sửa lỗi đăng nhập"})
        cmd = recv_until(ws, "command")
        assert cmd["name"] == "work" and "chưa chắc" in cmd["reply"]
        ws.send_json({"type": "ask", "text": "job hay old"})
        start = recv_until(ws, "task_start")
        assert (start["project"]["name"], start["request"]) == ("job-hay-old", "sửa lỗi đăng nhập")
        recv_until(ws, "task_end")
    assert launcher.calls == [(root / "job-hay-old", "sửa lỗi đăng nhập", None)]


def test_status_and_cancel_by_voice(agent_app):
    client, _, bridge, root = agent_app
    with client.websocket_connect("/ws") as ws:
        recv_until(ws, "tasks")
        ws.send_json({"type": "ask", "text": "tiến độ sao rồi"})   # chưa có việc nào, không ở tab Công việc: hỏi Claude
        recv_until(ws, "answer_end")
        set_watcher(client, FakeWatcher([Submitted("s3", root / "z.jsonl"), ToolStep("t1", "Read", "README.md")],
                                        block=True))
        ws.send_json({"type": "task", "text": "đọc README", "project": "job-hay"})
        recv_until(ws, "task_step")
        ws.send_json({"type": "ask", "text": "Claude làm tới đâu rồi"})
        reply = recv_until(ws, "command")["reply"]
        assert "job hay" in reply and "1 bước" in reply and "README.md" in reply
        ws.send_json({"type": "ask", "text": "ngừng theo dõi đi"})
        assert recv_until(ws, "task_end")["status"] == "cancelled"   # huỷ ngay, câu đáp đến sau
        assert "thôi theo dõi" in recv_until(ws, "command")["reply"]
    assert bridge.questions == ["tiến độ sao rồi"]


def test_match_work_query():
    from server.agent.tasks import match_work_query
    assert match_work_query("tiến độ thế nào") == "status"
    assert match_work_query("việc tới đâu rồi") == "status"
    assert match_work_query("Claude xong chưa?") == "status"
    assert match_work_query("ngừng theo dõi đi") == "cancel"
    assert match_work_query("cập nhật tiến độ trong PLAN.md") is None
    assert match_work_query("sửa lỗi này thế nào") is None
    assert match_work_query("viết test cho phần thanh toán, xong chưa thì báo mình rồi chạy lại toàn bộ") is None


def test_work_mode_stt_prompt_has_project_names(agent_app):
    client, *_ = agent_app

    def speak(ws):
        client.engine.calls.clear()
        ws.send_json({"type": "start", "topic": None})
        ws.send_bytes(pcm(1.0))
        ws.send_json({"type": "stop"})
        recv_until(ws, "transcript")
        return [c["prompt"] for c in client.engine.calls]

    with client.websocket_connect("/ws") as ws:
        recv_until(ws, "tasks")
        assert not any("Dự án:" in (p or "") for p in speak(ws))   # tab khác: prompt như cũ
        ws.send_json({"type": "work", "on": True, "project": None})
        prompts = speak(ws)
        assert prompts and all(p.endswith(".") and "Dự án:" in p for p in prompts)
        tail = prompts[0].split("Dự án:")[1]
        assert {"job hay", "job hay old", "fruit rush"} <= {n.strip(" .") for n in tail.split(",")}


# ---------- Sửa khi hiểu sai (Phase 8 D): "ừ", "cái thứ hai", "không", "sai rồi", nút Sai dự án ----------

def read_labels(tmp_path: Path) -> list[dict]:
    import json
    return [json.loads(line) for f in sorted((tmp_path / "logs").glob("intents-*.jsonl"))
            for line in f.read_text(encoding="utf-8").splitlines()]


def test_reply_kind():
    from server.agent.tasks import reply_kind
    assert reply_kind("ừ đúng rồi") == ("yes", "")
    assert reply_kind("Ok nhé.") == ("yes", "")
    assert reply_kind("cái thứ hai đó") == (1, "")
    assert reply_kind("số 1") == (0, "")
    assert reply_kind("không phải") == ("no", "")
    assert reply_kind("Sai rồi, job hay cơ") == ("no", "job hay cơ")
    assert reply_kind("job hay") == (None, "job hay")
    assert reply_kind("không cần test, sửa lỗi login") == ("no", "cần test, sửa lỗi login")


def test_choose_by_ordinal_and_confirm_by_yes(agent_app, tmp_path):
    client, launcher, bridge, root = agent_app
    set_watcher(client, FakeWatcher([NotSubmitted()]))
    with client.websocket_connect("/ws") as ws:
        recv_until(ws, "tasks")
        ws.send_json({"type": "ask", "text": "mở dự án job rồi sửa lỗi đăng nhập"})
        choices = recv_until(ws, "command")["choices"]
        assert len(choices) >= 2
        ws.send_json({"type": "ask", "text": "cái thứ hai nhé"})
        start = recv_until(ws, "task_start")
        assert (start["project"]["name"], start["request"]) == (choices[1], "sửa lỗi đăng nhập")
        recv_until(ws, "task_end")
        # Một ứng viên: hỏi "có phải ... không?", trả lời "ừ" là đủ
        ws.send_json({"type": "work", "on": True, "project": "job-hay"})
        ws.send_json({"type": "ask", "text": "mở dạng karaoke."})
        cmd = recv_until(ws, "command")
        assert cmd["choices"] == ["guitar-karaoke"] and "có phải" in cmd["reply"]
        ws.send_json({"type": "ask", "text": "ừ đúng rồi"})
        assert recv_until(ws, "task_start")["project"]["name"] == "guitar-karaoke"
        recv_until(ws, "task_end")
    assert launcher.calls[-1] == (root / "guitar-karaoke", None, None)
    labels = read_labels(tmp_path)
    assert [(r["via"], r["project"]) for r in labels] == [("choose", choices[1]), ("confirm", "guitar-karaoke")]
    assert labels[0]["heard"] == "mở dự án job rồi sửa lỗi đăng nhập" and labels[0]["query"] == "job"
    assert labels[0]["asked"] == choices and labels[0]["wrong"] is None
    assert labels[1]["heard"] == "mở dạng karaoke." and labels[1]["answer"] == "ừ đúng rồi"
    assert bridge.questions == []


def test_answer_no_then_name(agent_app, tmp_path):
    client, launcher, bridge, root = agent_app
    set_watcher(client, FakeWatcher([NotSubmitted()]))
    with client.websocket_connect("/ws") as ws:
        recv_until(ws, "tasks")
        ws.send_json({"type": "ask", "text": "mở dự án job rồi sửa lỗi đăng nhập"})
        recv_until(ws, "command")
        ws.send_json({"type": "ask", "text": "không phải"})
        cmd = recv_until(ws, "command")
        assert "dự án nào" in cmd["reply"] and "choices" not in cmd
        ws.send_json({"type": "ask", "text": "fruit rush"})
        start = recv_until(ws, "task_start")
        assert (start["project"]["name"], start["request"]) == ("fruit-rush", "sửa lỗi đăng nhập")
        recv_until(ws, "task_end")
    assert [(r["via"], r["project"], r["answer"]) for r in read_labels(tmp_path)] == [("name", "fruit-rush", "fruit rush")]
    assert bridge.questions == []


def test_deny_right_after_task_asks_again(agent_app, tmp_path):
    client, launcher, bridge, root = agent_app
    set_watcher(client, FakeWatcher([Submitted("s1", root / "a.jsonl")], block=True))
    with client.websocket_connect("/ws") as ws:
        recv_until(ws, "tasks")
        ws.send_json({"type": "ask", "text": "mở dự án job hay rồi thêm phân trang"})
        first = recv_until(ws, "task_start")
        assert first["project"]["name"] == "job-hay"
        assert recv_until(ws, "task_status")["status"] == "opening"
        assert recv_until(ws, "task_status")["status"] == "running"
        ws.send_json({"type": "ask", "text": "sai rồi"})
        end = recv_until(ws, "task_end")
        assert end["task_id"] == first["task_id"] and end["status"] == "cancelled"
        cmd = recv_until(ws, "command")
        assert "thôi theo dõi" in cmd["reply"] and "dừng tay" in cmd["reply"]
        assert cmd["choices"] == ["job-hay-old"]
        ws.send_json({"type": "ask", "text": "ừ"})
        start = recv_until(ws, "task_start")
        assert (start["project"]["name"], start["request"]) == ("job-hay-old", "thêm phân trang")
        # "không phải, fruit rush cơ": giao thẳng sang dự án vừa nói
        ws.send_json({"type": "ask", "text": "không phải, fruit rush cơ"})
        start = recv_until(ws, "task_start")
        assert (start["project"]["name"], start["request"]) == ("fruit-rush", "thêm phân trang")
    labels = read_labels(tmp_path)
    assert [(r["via"], r["project"], r["wrong"]) for r in labels] == [
        ("confirm", "job-hay-old", "job-hay"), ("deny_name", "fruit-rush", "job-hay-old")]
    assert labels[0]["heard"] == "mở dự án job hay rồi thêm phân trang"
    assert bridge.questions == []


def test_deny_without_recent_task_goes_to_claude(agent_app):
    client, launcher, bridge, _ = agent_app
    with client.websocket_connect("/ws") as ws:
        recv_until(ws, "tasks")
        ws.send_json({"type": "ask", "text": "sai rồi, EC2 không phải serverless"})
        recv_until(ws, "answer_end")
    assert bridge.questions == ["sai rồi, EC2 không phải serverless"] and launcher.calls == []


def test_task_fix_moves_task_and_labels(agent_app, tmp_path):
    client, launcher, bridge, root = agent_app
    set_watcher(client, FakeWatcher([Submitted("s2", root / "b.jsonl")], block=True))
    with client.websocket_connect("/ws") as ws:
        recv_until(ws, "tasks")
        ws.send_json({"type": "task", "text": "đọc README", "project": "job-hay"})
        first = recv_until(ws, "task_start")
        ws.send_json({"type": "task_fix", "task_id": first["task_id"], "project": "nope"})
        assert recv_until(ws, "error")["code"] == "bad_message"
        ws.send_json({"type": "task_fix", "task_id": first["task_id"], "project": "fruit-rush"})
        assert recv_until(ws, "task_end")["task_id"] == first["task_id"]
        start = recv_until(ws, "task_start")
        assert (start["project"]["name"], start["request"]) == ("fruit-rush", "đọc README")
    assert [c[:2] for c in launcher.calls] == [(root / "job-hay", "đọc README"), (root / "fruit-rush", "đọc README")]
    [label] = read_labels(tmp_path)
    assert (label["via"], label["project"], label["wrong"], label["request"]) == ("fix", "fruit-rush", "job-hay",
                                                                                   "đọc README")


def test_yes_without_question_is_not_a_task(agent_app):
    client, launcher, bridge, _ = agent_app
    with client.websocket_connect("/ws") as ws:
        recv_until(ws, "tasks")
        ws.send_json({"type": "work", "on": True, "project": "job-hay"})
        ws.send_json({"type": "ask", "text": "ừ"})
        assert recv_until(ws, "command")["name"] == "work"
    assert launcher.calls == [] and bridge.questions == []
