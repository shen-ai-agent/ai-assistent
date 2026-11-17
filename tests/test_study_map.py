"""Sơ đồ tư duy tab Học: đọc markdown thành cây, tạo sơ đồ bài / cả lớp chạy nền. Mapper giả, không gọi Claude."""

from __future__ import annotations

import asyncio
import os
import time

from fastapi.testclient import TestClient

from server.app import create_app
from server.config import ROOT, Config
from server.study import Library, MapService, parse_map
from server.study.mindmap import MAX_NODES

DOC = "# Amazon S3\nS3 là dịch vụ lưu trữ object của AWS. Bucket chứa object, mỗi object có key.\n"
LESSON_MAP = """# Amazon S3
- Khái niệm
  - Bucket :: thùng chứa object
  - !Object key :: tên đầy đủ, không phải thư mục thật
- Storage class
  - Standard
"""
COURSE_MAP = """Đây là sơ đồ:
# AWS
- Lưu trữ
  - S3 [B1] :: object storage
  - !EBS và S3 [B1, B2] :: block khác object
- Liên hệ giữa các bài
  - S3 → EC2 [B2, B9]
"""
PROMPTS = (ROOT / "prompts" / "study_mindmap.md", ROOT / "prompts" / "study_course_map.md")


# ---------- parse_map ----------

def test_parse_nested_bullets_notes_and_warn():
    tree = parse_map(LESSON_MAP, "bỏ qua")
    assert tree["label"] == "Amazon S3"
    concept, storage = tree["children"]
    assert concept["label"] == "Khái niệm" and storage["children"] == [{"label": "Standard"}]
    assert concept["children"][0] == {"label": "Bucket", "note": "thùng chứa object"}
    assert concept["children"][1] == {"label": "Object key", "note": "tên đầy đủ, không phải thư mục thật", "warn": True}


def test_parse_headings_bold_numbers_and_refs():
    md = "```markdown\n# Gốc\n## Nhánh **một**\n1. Ý A [B1]\n   - Ý A1 [b2; B7]\n## Nhánh hai\n* Ý B\n```"
    tree = parse_map(md, ref_map={"B1": "d1", "B2": "d2"})
    one, two = tree["children"]
    assert one["label"] == "Nhánh một" and two["children"] == [{"label": "Ý B"}]
    a = one["children"][0]
    assert a["label"] == "Ý A" and a["refs"] == ["d1"]
    assert a["children"] == [{"label": "Ý A1", "refs": ["d2"]}]   # B7 không có trong lớp: bỏ


def test_parse_without_root_uses_title_and_rejects_garbage():
    tree = parse_map("- A\n  - A1\n- B", "Bài 1")
    assert tree["label"] == "Bài 1" and [c["label"] for c in tree["children"]] == ["A", "B"]
    assert parse_map("Xin lỗi, mình không đọc được tài liệu.", "x") is None
    assert parse_map("# Gốc\n- chỉ một ý", "x") is None


def test_parse_dedent_and_limit():
    tree = parse_map("# G\n- A\n    - A1\n  - A2\n- B")   # thụt lề lệch: A2 vẫn là con của A
    a, b = tree["children"]
    assert [c["label"] for c in a["children"]] == ["A1", "A2"] and b["label"] == "B"
    big = "# G\n" + "\n".join(f"- ý {i}" for i in range(MAX_NODES + 50))
    assert len(parse_map(big)["children"]) == MAX_NODES


# ---------- MapService ----------

class FakeMapper:
    def __init__(self, outputs: dict[str, str]):
        self.outputs = outputs   # dòng đầu của system prompt chứa từ khoá → output
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, system: str, text: str) -> str:
        self.calls.append((system, text))
        await asyncio.sleep(0)
        return self.outputs["course" if "khoá học" in system else "lesson"]


async def _run(maps: MapService, start):
    out = start()
    await maps.wait()
    return out


def test_doc_map_uses_parts_and_questions(tmp_path):
    lib = Library(tmp_path)
    doc = lib.create(DOC)
    doc.parts = {"2": "Storage class", "1": "Bucket"}
    lib.save(doc)
    lib.append_history(doc.id, "tiếp", "…", 1)
    lib.append_history(doc.id, "object key khác thư mục chỗ nào vậy", "…", 1)
    mapper = FakeMapper({"lesson": LESSON_MAP})
    maps = MapService(lib, mapper, *PROMPTS)
    assert maps.doc_status(doc.id)["status"] == "none"

    async def go():
        first = maps.start_doc(doc.id)
        assert first["status"] == "running"
        maps.start_doc(doc.id)   # đang chạy: không chạy lượt thứ hai
        await maps.wait()

    asyncio.run(go())
    assert len(mapper.calls) == 1
    system, text = mapper.calls[0]
    assert "sơ đồ tư duy" in system and not system.startswith("#")
    assert "1. Bucket\n2. Storage class" in text and "- object key khác thư mục" in text
    assert "- tiếp" not in text and text.endswith(DOC)
    st = maps.doc_status(doc.id)
    assert st["status"] == "ready" and st["tree"]["label"] == "Amazon S3" and st["created"] > 0


def test_doc_map_bad_output_is_error(tmp_path):
    lib = Library(tmp_path)
    doc = lib.create(DOC)
    maps = MapService(lib, FakeMapper({"lesson": "Không biết."}), *PROMPTS)
    asyncio.run(_run(maps, lambda: maps.start_doc(doc.id)))
    st = maps.doc_status(doc.id)
    assert st["status"] == "error" and "không đúng dạng" in st["error"] and "tree" not in st


def test_course_map_builds_missing_lessons_then_merges(tmp_path):
    lib = Library(tmp_path)
    sub = lib.create_subject("AWS")
    d1 = lib.create(DOC, "S3", subject=sub.id)
    d2 = lib.create(DOC, "EC2", subject=sub.id)
    lib.create(DOC, "Ngoài lớp")
    mapper = FakeMapper({"lesson": LESSON_MAP, "course": COURSE_MAP})
    maps = MapService(lib, mapper, *PROMPTS)
    st = maps.course_status(sub.id)
    assert st["status"] == "none" and [x["title"] for x in st["lessons"]] == ["S3", "EC2"]

    asyncio.run(_run(maps, lambda: maps.start_course(sub.id)))
    assert len(mapper.calls) == 3   # hai bài rồi gộp
    course_text = mapper.calls[2][1]
    assert '"AWS" (2 bài' in course_text and "===== B1: S3 =====" in course_text and "===== B2: EC2" in course_text
    st = maps.course_status(sub.id)
    assert st["status"] == "ready" and not st["stale"] and all(x["map"] for x in st["lessons"])
    store = st["tree"]["children"][0]
    assert store["children"][1] == {"label": "EBS và S3", "note": "block khác object", "warn": True,
                                    "refs": [d1.id, d2.id]}
    assert st["refs"] == [{"ref": "B1", "id": d1.id, "title": "S3"}, {"ref": "B2", "id": d2.id, "title": "EC2"}]

    # Sơ đồ một bài tạo lại sau đó: sơ đồ lớp đã cũ; tạo lại lớp thì không tạo lại sơ đồ bài
    path = lib.doc_map_path(d2.id)
    later = time.time() + 5
    os.utime(path, (later, later))
    assert maps.course_status(sub.id)["stale"]
    asyncio.run(_run(maps, lambda: maps.start_course(sub.id)))
    assert len(mapper.calls) == 4 and not maps.course_status(sub.id)["stale"]

    # Thêm bài: cũ; xoá bài: liên kết tới bài đó biến mất
    d3 = lib.create(DOC, "VPC", subject=sub.id)
    assert maps.course_status(sub.id)["stale"]
    lib.delete(d1.id)
    st = maps.course_status(sub.id)
    assert [r["ref"] for r in st["refs"]] == ["B2"] and "refs" not in st["tree"]["children"][0]["children"][0]
    assert d3.id in {x["id"] for x in st["lessons"]}

    lib.delete_subject(sub.id)
    assert not any(p.exists() for p in lib.course_map_paths(sub.id))


def test_course_map_lesson_failure_stops(tmp_path):
    lib = Library(tmp_path)
    sub = lib.create_subject("AWS")
    lib.create(DOC, "S3", subject=sub.id)
    mapper = FakeMapper({"lesson": "lỗi", "course": COURSE_MAP})
    maps = MapService(lib, mapper, *PROMPTS)
    asyncio.run(_run(maps, lambda: maps.start_course(sub.id)))
    st = maps.course_status(sub.id)
    assert st["status"] == "error" and 'bài "S3"' in st["error"] and len(mapper.calls) == 1


def test_course_map_lessons_in_parallel_keep_successes(tmp_path):
    lib = Library(tmp_path)
    sub = lib.create_subject("AWS")
    docs = [lib.create(DOC, t, subject=sub.id) for t in ("S3", "Hỏng", "EC2")]
    broken = {"Hỏng"}
    running = peak = 0

    async def mapper(system: str, text: str) -> str:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        if "khoá học" in system:
            return COURSE_MAP
        return "lỗi" if any(f'Bài: "{t}"' in text for t in broken) else LESSON_MAP

    maps = MapService(lib, mapper, *PROMPTS)
    asyncio.run(_run(maps, lambda: maps.start_course(sub.id)))
    st = maps.course_status(sub.id)
    assert peak == 3   # ba bài tạo cùng lúc
    assert st["status"] == "error" and 'bài "Hỏng"' in st["error"]
    assert [x["map"] for x in st["lessons"]] == [True, False, True]   # bài xong vẫn giữ sơ đồ

    broken.clear()
    asyncio.run(_run(maps, lambda: maps.start_course(sub.id)))
    st = maps.course_status(sub.id)
    assert st["status"] == "ready" and peak == 3 and len(st["refs"]) == len(docs)


# ---------- REST ----------

def test_map_api(tmp_path):
    from tests.test_study import FakeTutor, _Engine

    cfg = Config(log_dir=tmp_path / "logs", save_audio=False, bridge_log_dir=tmp_path / "sessions",
                 study_dir=tmp_path / "study")
    mapper = FakeMapper({"lesson": LESSON_MAP, "course": COURSE_MAP})
    app = create_app(cfg, engine_factory=lambda _cfg: _Engine(), bridge_factory=lambda _cfg: None,
                     tutor_factory=lambda _cfg: (lambda workdir: FakeTutor(workdir, [])),
                     mapper_factory=lambda _cfg: mapper)

    def wait(url):
        for _ in range(200):
            st = client.get(url).json()
            if st["status"] != "running":
                return st
            time.sleep(0.01)
        raise AssertionError("sơ đồ không xong")

    with TestClient(app) as client:
        sub = client.post("/api/study/subjects", json={"name": "AWS"}).json()
        assert client.post(f"/api/study/subjects/{sub['id']}/map").status_code == 400   # lớp trống
        doc = client.post("/api/study", json={"text": DOC, "subject": sub["id"]}).json()
        assert client.get(f"/api/study/{doc['id']}/map").json()["status"] == "none"
        client.post(f"/api/study/{doc['id']}/map")
        assert wait(f"/api/study/{doc['id']}/map")["tree"]["label"] == "Amazon S3"
        client.post(f"/api/study/subjects/{sub['id']}/map")
        st = wait(f"/api/study/subjects/{sub['id']}/map")
        assert st["status"] == "ready" and st["title"] == "AWS" and len(mapper.calls) == 2
        assert client.get("/api/study/subjects/s-khong-co/map").status_code == 404
        assert client.get("/api/study/20260101-000000/map").status_code == 404
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["study"]["maps"] is True
