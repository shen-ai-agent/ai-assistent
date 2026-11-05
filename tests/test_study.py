"""Tab Học (T-012): thư viện tài liệu, cắt dòng tiến độ, gia sư theo tài liệu. Gia sư giả, không gọi Claude thật."""

from __future__ import annotations

import asyncio
import base64
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.bridge import Done, TextDelta
from server.config import Config
from server.study import Library, MarkerFilter, StudyError, StudyService, extract_text
from server.study.library import MAX_LINE, clean_text, docx_text

DOC = "# Amazon S3\nS3 là dịch vụ lưu trữ object của AWS. Bucket chứa object, mỗi object có key.\n"


def feed(deltas: list[str]) -> tuple[str, MarkerFilter]:
    f = MarkerFilter()
    out = "".join(f.push(d) for d in deltas) + f.flush()
    return out, f


# ---------- MarkerFilter ----------

def test_marker_cut_across_deltas():
    f = MarkerFilter()
    assert f.push("[Phần 2") == ""
    assert f.push("/5: Bucket và object]\nBucket là") == "Bucket là"
    assert f.found == (2, 5, "Bucket và object")
    assert f.push(" cái thùng.") == " cái thùng."


def test_marker_absent_passes_text_through():
    out, f = feed(["Câu hỏi hay. ", "S3 có 11 số 9."])
    assert out == "Câu hỏi hay. S3 có 11 số 9." and f.found is None


def test_bracket_that_is_not_marker_is_kept():
    out, f = feed(["[Lưu ý] ", "đây không phải dòng tiến độ."])
    assert out == "[Lưu ý] đây không phải dòng tiến độ." and f.found is None


def test_marker_without_title_and_leading_space():
    out, f = feed(["\n [phần 1/3]\n", "Mở đầu."])
    assert out == "Mở đầu." and f.found == (1, 3, "")


def test_unfinished_marker_flushed_at_end():
    out, f = feed(["[Phần 1"])
    assert out == "[Phần 1" and f.found is None


# ---------- Chuyển file ----------

def make_docx(body_xml: str) -> bytes:
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {ns}><w:body>{body_xml}</w:body></w:document>')
    return buf.getvalue()


def p(text: str, style: str | None = None, numbered: bool = False) -> str:
    ppr = ""
    if style or numbered:
        ppr = "<w:pPr>" + (f'<w:pStyle w:val="{style}"/>' if style else "") + \
              ("<w:numPr><w:ilvl w:val=\"0\"/></w:numPr>" if numbered else "") + "</w:pPr>"
    return f"<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>"


def test_docx_text_headings_lists_tables():
    row = lambda a, b: "<w:tr><w:tc>" + p(a) + "</w:tc><w:tc>" + p(b) + "</w:tc></w:tr>"
    table = "<w:tbl>" + row("Lớp", "Giá") + row("Glacier", "rẻ | chậm") + "</w:tbl>"
    data = make_docx(p("S3 Storage Classes", "Heading1") + p("Standard dùng cho dữ liệu hay đọc.")
                     + p("Glacier", numbered=True) + table)
    assert docx_text(data).splitlines() == ["# S3 Storage Classes", "Standard dùng cho dữ liệu hay đọc.",
                                           "- Glacier", "| Lớp | Giá |", "|---|---|", "| Glacier | rẻ / chậm |"]


def test_extract_text_encodings_and_errors():
    assert extract_text("a.md", "Xin chào".encode("utf-8-sig")) == "Xin chào"
    assert extract_text("a.txt", "Xin chào".encode("utf-16")) == "Xin chào"
    with pytest.raises(StudyError, match=".pdf"):
        extract_text("a.pdf", b"%PDF")
    with pytest.raises(StudyError, match="docx"):
        extract_text("a.docx", b"not a zip")


def test_clean_text_wraps_long_lines():
    text = clean_text(("từ " * 800) + "\r\n\r\n\r\n\r\nhết")
    lines = text.splitlines()
    assert max(map(len, lines)) <= MAX_LINE
    assert lines[-2:] == ["", "hết"]


# ---------- Library ----------

def test_library_crud(tmp_path):
    lib = Library(tmp_path, max_chars=1000)
    doc = lib.create(DOC)
    assert doc.title == "Amazon S3" and doc.source == "paste"
    assert lib.text(doc.id) == DOC
    second = lib.create(DOC, title="Bài 2", source="s3.docx")
    assert second.id != doc.id and second.title == "Bài 2"
    assert [d.id for d in lib.all()] == [second.id, doc.id]
    lib.set_part(doc, 2, 4, "Bucket")
    assert lib.get(doc.id).parts == {"2": "Bucket"} and [d.id for d in lib.all()][0] == doc.id
    lib.append_history(doc.id, "tiếp", "Phần hai.", 2)
    assert lib.history(doc.id)[0]["answer"] == "Phần hai."
    doc.session_id = "abc"
    lib.save(doc)
    reset = lib.reset(doc.id)
    assert (reset.session_id, reset.part, reset.total, reset.parts) == (None, 0, 0, {})
    assert lib.history(doc.id) == [] and lib.text(doc.id) == DOC
    lib.delete(doc.id)
    assert [d.id for d in lib.all()] == [second.id]
    assert "session_id" not in second.public() and second.public()["started"] is False


def test_library_rejects_bad_input(tmp_path):
    lib = Library(tmp_path, max_chars=100)
    with pytest.raises(StudyError, match="ngắn"):
        lib.create("   ")
    with pytest.raises(StudyError, match="giới hạn"):
        lib.create("x" * 200)
    for bad in ("../etc", "", "20260101-000000/../x"):
        with pytest.raises(StudyError):
            lib.get(bad)


# ---------- StudyService ----------

class FakeTutor:
    """Như bridge: ask() stream deltas, xong thì đặt session_id. Ghi lại prompt và workdir."""

    name = "fake_tutor"

    def __init__(self, workdir, deltas):
        self.workdir, self.deltas = workdir, deltas
        self.session_id: str | None = None
        self.model = ""
        self.prompts: list[str] = []

    def new_session(self):
        self.session_id = None

    async def ask(self, text):
        self.prompts.append(text)
        for d in self.deltas:
            yield TextDelta(d)
        self.session_id = f"sess-{len(self.prompts)}"
        yield Done(self.session_id, 0.02, "claude-fake")


LESSON = ["[Phần 1/3: S3 là gì]\n", "S3 giống một kho chứa đồ. ", "Mình sang phần tiếp nhé?\n---CODE---\n- bucket"]


async def run_ask(study, doc_id, question, model=None):
    seen = []

    async def on_doc(doc):
        seen.append((doc.part, doc.total))

    events = [ev async for ev in study.ask(doc_id, question, model, on_doc)]
    return events, seen


def test_service_first_turn_reads_doc_then_resumes(tmp_path):
    lib = Library(tmp_path)
    doc = lib.create(DOC)
    tutors = []

    def factory(workdir):
        tutors.append(FakeTutor(workdir, LESSON))
        return tutors[-1]

    study = StudyService(lib, factory)
    events, seen = asyncio.run(run_ask(study, doc.id, "Bắt đầu nhé", model="haiku"))
    tutor = tutors[0]
    assert tutor.workdir == tmp_path / doc.id and tutor.model == "haiku"
    assert "doc.md" in tutor.prompts[0] and tutor.prompts[0].endswith("Bắt đầu nhé")
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text.startswith("S3 giống một kho") and "[Phần" not in text
    assert seen == [(1, 3), (1, 3)]
    saved = lib.get(doc.id)
    assert (saved.session_id, saved.part, saved.total, saved.parts) == ("sess-1", 1, 3, {"1": "S3 là gì"})
    assert lib.history(doc.id) == [{"at": lib.history(doc.id)[0]["at"], "question": "Bắt đầu nhé",
                                    "answer": text, "part": 1}]

    asyncio.run(run_ask(study, doc.id, "tiếp"))
    assert tutor.prompts[1] == "tiếp" and len(tutors) == 1

    # Server khởi động lại: gia sư mới lấy session đã lưu, không gửi lại lời dặn đọc tài liệu
    study2 = StudyService(lib, factory)
    asyncio.run(run_ask(study2, doc.id, "giảng lại"))
    assert tutors[1].prompts == ["giảng lại"]

    lib.reset(doc.id)
    study2.forget(doc.id)
    asyncio.run(run_ask(study2, doc.id, "Bắt đầu"))
    assert "doc.md" in tutors[2].prompts[0]


# ---------- WebSocket + REST ----------

class MainBridge(FakeTutor):
    name = "fake"


def study_client(tmp_path, tutors):
    cfg = Config(log_dir=tmp_path / "logs", save_audio=False, min_speech_ms=0, bridge_log_dir=tmp_path / "sessions",
                 study_dir=tmp_path / "study")
    main = MainBridge(None, ["Tab Mimi trả lời."])

    def make(workdir):
        tutors.append(FakeTutor(workdir, LESSON))
        return tutors[-1]

    app = create_app(cfg, engine_factory=lambda _cfg: _Engine(), bridge_factory=lambda _cfg: main,
                     tutor_factory=lambda _cfg: make)
    return TestClient(app), main


class _Engine:
    def transcribe(self, audio, **_):
        from server.stt.engine import Transcript
        return Transcript("giảng lại phần này", "vi", 1.0)


def until(ws, kind):
    msgs = []
    while True:
        msg = ws.receive_json()
        msgs.append(msg)
        if msg["type"] == kind:
            return msgs


def test_study_tab_over_websocket(tmp_path):
    tutors = []
    client, main = study_client(tmp_path, tutors)
    with client:
        doc = client.post("/api/study", json={"text": DOC}).json()
        assert client.get("/api/study").json()["docs"][0]["id"] == doc["id"]
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["study"] == {"max_chars": 200_000}
            ws.send_json({"type": "study", "doc_id": doc["id"]})
            assert until(ws, "study_doc")[-1]["doc"]["id"] == doc["id"]
            ws.send_json({"type": "ask", "text": "Bắt đầu nhé"})
            msgs = until(ws, "answer_end")
            start = next(m for m in msgs if m["type"] == "answer_start")
            assert start["doc_id"] == doc["id"]
            text = "".join(m["text"] for m in msgs if m["type"] == "answer_delta")
            assert text.startswith("S3 giống") and "[Phần" not in text
            assert any(m["type"] == "study_doc" and m["doc"]["part"] == 1 for m in msgs)
            assert main.prompts == []   # không hỏi Claude của tab Mimi

            ws.send_json({"type": "study", "doc_id": None})
            ws.send_json({"type": "ask", "text": "EC2 là gì"})
            msgs = until(ws, "answer_end")
            assert next(m for m in msgs if m["type"] == "answer_start")["doc_id"] is None
            assert main.prompts == ["EC2 là gì"]

            ws.send_json({"type": "study", "doc_id": "20990101-000000"})
            assert until(ws, "error")[-1]["code"] == "bad_message"

        full = client.get(f"/api/study/{doc['id']}").json()
        assert full["text"] == DOC and full["part"] == 1 and full["history"][0]["question"] == "Bắt đầu nhé"
        assert client.post(f"/api/study/{doc['id']}/reset").json()["started"] is False
        assert client.patch(f"/api/study/{doc['id']}", json={"title": "S3 cơ bản"}).json()["title"] == "S3 cơ bản"
        assert client.delete(f"/api/study/{doc['id']}").json() == {"ok": True}
        assert client.get(f"/api/study/{doc['id']}").status_code == 404


def test_study_upload_docx_and_errors(tmp_path):
    client, _ = study_client(tmp_path, [])
    with client:
        data = make_docx(p("IELTS Reading", "Heading1") + p("The passage discusses urban farming in detail."))
        doc = client.post("/api/study", json={"filename": "reading.docx",
                                              "data_b64": base64.b64encode(data).decode()}).json()
        assert doc["title"] == "reading" and doc["source"] == "reading.docx"
        assert client.get(f"/api/study/{doc['id']}").json()["text"].startswith("# IELTS Reading")
        bad = client.post("/api/study", json={"filename": "a.pdf", "data_b64": base64.b64encode(b"%PDF").decode()})
        assert bad.status_code == 400 and ".pdf" in bad.json()["detail"]
        assert client.post("/api/study", json={}).status_code == 400
        assert client.get("/api/study/..%2Fx").status_code == 404


def test_study_disabled_without_claude_cli(tmp_path):
    cfg = Config(log_dir=tmp_path / "logs", save_audio=False)
    with TestClient(create_app(cfg, engine_factory=lambda _cfg: _Engine())) as client:
        assert client.get("/api/study").status_code == 503
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["study"] is None
            ws.send_json({"type": "study", "doc_id": "20260101-000000"})
            assert until(ws, "error")[-1]["code"] == "study_disabled"
