"""REST /api/study cho tab Học: thêm, xem, đổi tên, chuyển lớp, học lại, xoá, sắp xếp (PUT /order) tài liệu;
/api/study/subjects: lớp học (môn) để nhóm tài liệu. Giảng bài đi qua WebSocket (message study).
Sơ đồ tư duy: GET/POST /{doc_id}/map và /subjects/{sid}/map (POST bắt đầu tạo nền, GET hỏi trạng thái + cây).

File tải lên gửi bằng JSON base64 (không cần python-multipart): {"filename", "data_b64", "title"?};
dán text: {"text", "title"?}. Cả hai nhận thêm "subject" (id lớp) để thêm thẳng vào lớp.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import asdict

from fastapi import APIRouter, Body, HTTPException, Request

from .library import StudyError, extract_text
from .service import StudyService

router = APIRouter(prefix="/api/study")
MAX_UPLOAD = 10 * 2**20


def _study(request: Request) -> StudyService:
    study = request.app.state.study
    if study is None:
        raise HTTPException(503, "Tab Học cần [bridge] kind = \"claude_cli\" trong server/config.toml")
    return study


def _maps(request: Request):
    maps = _study(request).maps
    if maps is None:
        raise HTTPException(503, "Sơ đồ tư duy chưa bật")
    return maps


def _guard(fn, *args):
    try:
        return fn(*args)
    except StudyError as e:
        raise HTTPException(404 if str(e).startswith(("Không có tài liệu", "Không có lớp")) else 400, str(e)) from None


def _listing(lib, docs) -> dict:
    return {"docs": [d.public() for d in docs], "manual": lib.manual_order,
            "subjects": [asdict(s) for s in lib.subjects()]}


@router.get("")
async def list_docs(request: Request):
    lib = _study(request).library
    return _listing(lib, lib.all())


@router.put("/order")
async def reorder_docs(request: Request, body: dict = Body(...)):
    """Thứ tự kéo thả trong thư viện: {"ids": [...]}, trả lại danh sách đã sắp xếp."""
    lib = _study(request).library
    return _listing(lib, _guard(lib.reorder, body.get("ids")))


# ---------- Lớp học (khai báo trước /{doc_id}) ----------

@router.post("/subjects")
async def create_subject(request: Request, body: dict = Body(...)):
    return asdict(_guard(_study(request).library.create_subject, body.get("name")))


@router.put("/subjects/order")
async def reorder_subjects(request: Request, body: dict = Body(...)):
    return {"subjects": [asdict(s) for s in _guard(_study(request).library.reorder_subjects, body.get("ids"))]}


@router.patch("/subjects/{sid}")
async def rename_subject(request: Request, sid: str, body: dict = Body(...)):
    return asdict(_guard(_study(request).library.rename_subject, sid, body.get("name")))


@router.delete("/subjects/{sid}")
async def delete_subject(request: Request, sid: str):
    """Tài liệu trong lớp chuyển về chưa xếp lớp."""
    _guard(_study(request).library.delete_subject, sid)
    return {"ok": True}


@router.get("/subjects/{sid}/map")
async def subject_map(request: Request, sid: str):
    return _guard(_maps(request).course_status, sid)


@router.post("/subjects/{sid}/map")
async def make_subject_map(request: Request, sid: str):
    """Tạo (lại) sơ đồ cả lớp, tạo trước sơ đồ các bài còn thiếu. Chạy nền, trả trạng thái ngay."""
    return _guard(_maps(request).start_course, sid)


@router.post("")
async def create_doc(request: Request, body: dict = Body(...)):
    lib = _study(request).library
    title = body.get("title") or ""
    subject = body.get("subject")
    if subject is not None and not isinstance(subject, str):
        raise HTTPException(400, "subject phải là id lớp")
    if body.get("data_b64") is not None:
        filename = str(body.get("filename") or "")
        try:
            data = base64.b64decode(body["data_b64"], validate=True)
        except (binascii.Error, TypeError, ValueError):
            raise HTTPException(400, "File gửi lên bị hỏng") from None
        if len(data) > MAX_UPLOAD:
            raise HTTPException(400, f"File lớn quá {MAX_UPLOAD // 2**20}MB")
        text = _guard(extract_text, filename, data)
        doc = _guard(lambda: lib.create(text, str(title), filename, subject))
    elif isinstance(body.get("text"), str):
        doc = _guard(lambda: lib.create(body["text"], str(title), "paste", subject))
    else:
        raise HTTPException(400, "Cần text hoặc file")
    return doc.public()


@router.get("/{doc_id}")
async def get_doc(request: Request, doc_id: str):
    lib = _study(request).library
    doc = _guard(lib.get, doc_id)
    return {**doc.public(), "text": lib.text(doc_id), "history": lib.history(doc_id)}


@router.get("/{doc_id}/map")
async def doc_map(request: Request, doc_id: str):
    return _guard(_maps(request).doc_status, doc_id)


@router.post("/{doc_id}/map")
async def make_doc_map(request: Request, doc_id: str):
    return _guard(_maps(request).start_doc, doc_id)


@router.patch("/{doc_id}")
async def update_doc(request: Request, doc_id: str, body: dict = Body(...)):
    """{"title"?} đổi tên, {"subject"?} chuyển lớp (null = chưa xếp lớp)."""
    lib = _study(request).library
    doc = _guard(lib.get, doc_id)
    if "title" in body:
        doc = _guard(lib.rename, doc_id, str(body.get("title") or ""))
    if "subject" in body:
        doc = _guard(lib.move, doc_id, body["subject"])
    return doc.public()


@router.post("/{doc_id}/reset")
async def reset_doc(request: Request, doc_id: str):
    study = _study(request)
    doc = _guard(study.library.reset, doc_id)
    study.forget(doc_id)
    return doc.public()


@router.delete("/{doc_id}")
async def delete_doc(request: Request, doc_id: str):
    study = _study(request)
    _guard(study.library.delete, doc_id)
    study.forget(doc_id)
    return {"ok": True}
