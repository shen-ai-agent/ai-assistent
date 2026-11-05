"""REST /api/study cho tab Học: thêm, xem, đổi tên, học lại, xoá tài liệu. Giảng bài đi qua WebSocket (message study).

File tải lên gửi bằng JSON base64 (không cần python-multipart): {"filename", "data_b64", "title"?};
dán text: {"text", "title"?}.
"""

from __future__ import annotations

import base64
import binascii

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


def _guard(fn, *args):
    try:
        return fn(*args)
    except StudyError as e:
        raise HTTPException(400 if "Không có tài liệu" not in str(e) else 404, str(e)) from None


@router.get("")
async def list_docs(request: Request):
    return {"docs": [d.public() for d in _study(request).library.all()]}


@router.post("")
async def create_doc(request: Request, body: dict = Body(...)):
    lib = _study(request).library
    title = body.get("title") or ""
    if body.get("data_b64") is not None:
        filename = str(body.get("filename") or "")
        try:
            data = base64.b64decode(body["data_b64"], validate=True)
        except (binascii.Error, TypeError, ValueError):
            raise HTTPException(400, "File gửi lên bị hỏng") from None
        if len(data) > MAX_UPLOAD:
            raise HTTPException(400, f"File lớn quá {MAX_UPLOAD // 2**20}MB")
        text = _guard(extract_text, filename, data)
        doc = _guard(lambda: lib.create(text, str(title), filename))
    elif isinstance(body.get("text"), str):
        doc = _guard(lambda: lib.create(body["text"], str(title), "paste"))
    else:
        raise HTTPException(400, "Cần text hoặc file")
    return doc.public()


@router.get("/{doc_id}")
async def get_doc(request: Request, doc_id: str):
    lib = _study(request).library
    doc = _guard(lib.get, doc_id)
    return {**doc.public(), "text": lib.text(doc_id), "history": lib.history(doc_id)}


@router.patch("/{doc_id}")
async def rename_doc(request: Request, doc_id: str, body: dict = Body(...)):
    return _guard(_study(request).library.rename, doc_id, str(body.get("title") or "")).public()


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
