"""REST API cho trang duyệt đề xuất (web/learn.html). Logic nằm ở server/learning.py."""

from __future__ import annotations

import asyncio
from collections import Counter

from fastapi import APIRouter, Body, HTTPException, Request

from . import learning as L

router = APIRouter(prefix="/api/learn")


def _learn_dir(request: Request):
    return request.app.state.cfg.log_dir.parent / "learning"  # _local/learning: các quyết định duyệt/bỏ


def _context(request: Request):
    stt = request.app.state.stt
    turns = L.load_turns(request.app.state.cfg.log_dir)
    suggestions = L.suggest(turns, stt.glossary(), stt.corrector().pairs, L.decided_ids(_learn_dir(request)))
    return stt, turns, suggestions


def _find(suggestions: list[L.Suggestion], sid: str) -> L.Suggestion:
    for s in suggestions:
        if s.id == sid:
            return s
    raise HTTPException(404, "Không còn đề xuất này (đã duyệt/bỏ, hoặc log đã đổi)")


@router.get("")
async def overview(request: Request):
    stt, turns, suggestions = _context(request)
    checks = request.app.state.learn_checks
    fixed = [t for t in turns if t.fixed is not None]
    return {
        "stats": {"turns": len(turns), "fixed": len(fixed),
                  "fixed_by_split": Counter(t.split for t in fixed), "min_turns": L.MIN_TURNS},
        "topics": stt.topics(),
        "suggestions": [{**s.to_dict(), "checks": {k[1] or "": v for k, v in checks.items() if k[0] == s.id}}
                        for s in suggestions],
    }


@router.post("/{sid}/check")
async def check(request: Request, sid: str, topic: str | None = Body(None, embed=True)):
    stt, turns, suggestions = _context(request)
    s = _find(suggestions, sid)
    if s.kind == "rule":
        result = L.check_rule(s, turns, stt.corrector().pairs)
        topic = None
    else:
        if topic not in stt.topics():
            raise HTTPException(400, "Chọn chủ đề cho thuật ngữ")
        async with request.app.state.gpu_lock:  # dùng chung GPU với lượt nói: xếp hàng
            result = await asyncio.to_thread(L.check_term, s, topic, stt, turns)
    request.app.state.learn_checks[(sid, topic)] = result
    return result


@router.post("/{sid}/approve")
async def approve(request: Request, sid: str, topic: str | None = Body(None, embed=True)):
    stt, _, suggestions = _context(request)
    s = _find(suggestions, sid)
    key = (sid, topic if s.kind == "term" else None)
    result = request.app.state.learn_checks.get(key)
    if result is None:
        raise HTTPException(409, "Cần chạy kiểm tra trước khi duyệt")
    if not result["passed"]:
        raise HTTPException(409, f"Kiểm tra không đạt: {result['reason']}")
    L.apply(s, key[1], stt.glossary_path, stt.corrections_path)
    L.record(_learn_dir(request), s, "approved", topic=key[1], check=result)
    return {"ok": True}


@router.post("/{sid}/reject")
async def reject(request: Request, sid: str):
    _, _, suggestions = _context(request)
    L.record(_learn_dir(request), _find(suggestions, sid), "rejected")
    return {"ok": True}
