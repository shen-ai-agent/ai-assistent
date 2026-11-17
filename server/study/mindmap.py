"""Sơ đồ tư duy (mindmap) cho tab Học: sơ đồ một bài, và sơ đồ cả lớp gộp từ sơ đồ các bài.

  <id>/mindmap.md      sơ đồ của bài (markdown, định dạng trong prompts/study_mindmap.md)
  maps/<sid>.md        sơ đồ cả lớp (prompts/study_course_map.md), `[B2]` cuối nhãn = ý nằm ở bài thứ 2
  maps/<sid>.json      B1, B2… là bài nào, và sơ đồ bài đã dùng (mtime) để biết sơ đồ lớp đã cũ chưa

Tạo sơ đồ là một lượt claude -p độc lập (Mapper, không tool, không session), chạy nền vì mất 30 giây tới
vài phút; web hỏi trạng thái bằng GET. Tạo sơ đồ lớp thì tạo trước sơ đồ các bài còn thiếu.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from ..bridge.claude_cli import load_prompt
from .library import Library, StudyError, _read_json

log = logging.getLogger("study.map")

Mapper = Callable[[str, str], Awaitable[str]]   # (system prompt, tin nhắn) → text Claude viết

MAX_NODES = 400          # Claude viết quá dài thì cắt, web vẽ vài trăm nút vẫn mượt
MIN_NODES = 3            # ít hơn thì coi như Claude không trả sơ đồ
MAP_SLOTS = 3            # số lượt claude tạo sơ đồ chạy cùng lúc (lớp nhiều bài chưa có sơ đồ)
MAX_QUESTIONS = 40      # số câu người học đã hỏi gửi kèm khi tạo sơ đồ bài
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^(\s*)(?:[-*+•]|\d+[.)])\s+(.*)$")
_REFS = re.compile(r"\[\s*(B\d+(?:\s*[,;]\s*B\d+)*)\s*\]", re.IGNORECASE)


class MapError(StudyError):
    """Tạo sơ đồ không được (Claude lỗi, trả sai định dạng, lớp trống)."""


@dataclass
class Node:
    label: str
    note: str = ""
    warn: bool = False                      # `!` đầu nhãn: ý dễ nhầm / quan trọng
    refs: list[str] = field(default_factory=list)   # id các bài chứa ý này (sơ đồ lớp)
    children: list[Node] = field(default_factory=list)

    def to_dict(self) -> dict:
        d: dict = {"label": self.label}
        if self.note:
            d["note"] = self.note
        if self.warn:
            d["warn"] = True
        if self.refs:
            d["refs"] = self.refs
        if self.children:
            d["children"] = [c.to_dict() for c in self.children]
        return d


def _node(text: str, ref_map: dict[str, str]) -> Node | None:
    """Một dòng "!Nhãn [B1, B3] :: ghi chú" → Node; nhãn rỗng thì None."""
    text = text.replace("**", "").replace("__", "").strip()
    found: list[str] = []

    def take(m: re.Match) -> str:
        found.extend(r.strip().upper() for r in re.split(r"[,;]", m.group(1)))
        return " "

    text = _REFS.sub(take, text)
    label, _, note = text.partition("::")
    label, note = " ".join(label.split()), " ".join(note.split())
    warn = label.startswith("!")
    label = label.lstrip("!").strip().rstrip(":").strip()
    if not label:
        return None
    refs = list(dict.fromkeys(ref_map[r] for r in found if r in ref_map))
    return Node(label, note, warn, refs)


def parse_map(md: str, title: str = "", ref_map: dict[str, str] | None = None) -> dict | None:
    """Markdown Claude viết → cây {label, note?, warn?, refs?, children?}. None nếu không ra sơ đồ.

    `#` đầu tiên là gốc (không có thì gốc là title); heading sâu hơn và gạch đầu dòng là nút con, độ sâu theo
    cấp heading rồi theo thụt lề. Dòng chữ thường (lời mở đầu), dòng ``` bị bỏ qua.
    """
    ref_map = ref_map or {}
    root = Node(title or "Sơ đồ")
    # (khoá, nút): heading (0, cấp), gạch đầu dòng (1, thụt lề); khoá nhỏ hơn là tầng cha
    stack: list[tuple[tuple[int, int], Node]] = [((-1, -1), root)]
    has_root = False
    count = 0
    for raw in md.splitlines():
        line = raw.expandtabs(4).rstrip()
        if m := _HEADING.match(line.strip()):
            key, text = (0, len(m.group(1))), m.group(2)
            if not has_root and count == 0:
                if node := _node(text, ref_map):
                    root.label, root.note, root.warn = node.label, node.note, node.warn
                    stack[0] = (key, root)
                    has_root = True
                continue
        elif m := _BULLET.match(line):
            key, text = (1, len(m.group(1))), m.group(2)
        else:
            continue
        node = _node(text, ref_map)
        if node is None:
            continue
        while len(stack) > 1 and stack[-1][0] >= key:
            stack.pop()
        stack[-1][1].children.append(node)
        stack.append((key, node))
        count += 1
        if count >= MAX_NODES:
            break
    return root.to_dict() if count >= MIN_NODES else None


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _questions(history: list[dict]) -> list[str]:
    """Câu người học tự hỏi (bỏ "tiếp", "ok"… và nút bấm nhanh): chỗ chưa hiểu, sơ đồ nên làm rõ."""
    out: list[str] = []
    for h in history:
        q = " ".join(str(h.get("question") or "").split())
        if len(q.split()) >= 4 and q not in out:
            out.append(q[:200])
    return out[-MAX_QUESTIONS:]


class MapService:
    def __init__(self, library: Library, mapper: Mapper, lesson_prompt: Path, course_prompt: Path):
        self.library = library
        self.mapper = mapper
        self.lesson_prompt = lesson_prompt
        self.course_prompt = course_prompt
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._errors: dict[str, str] = {}
        self._progress: dict[str, str] = {}

    # ---------- Trạng thái ----------

    def _job(self, key: str) -> dict:
        running = key in self._tasks
        out: dict = {"status": "running" if running else "none"}
        if running and (p := self._progress.get(key)):
            out["progress"] = p
        if not running and (e := self._errors.get(key)):
            out["status"], out["error"] = "error", e
        return out

    def doc_status(self, doc_id: str) -> dict:
        doc = self.library.get(doc_id)
        out = {**self._job(f"doc:{doc_id}"), "title": doc.title, "subject": doc.subject}
        path = self.library.doc_map_path(doc_id)
        if (created := _mtime(path)) is not None and (tree := parse_map(path.read_text(encoding="utf-8"), doc.title)):
            out.update(tree=tree, created=created)
            if out["status"] == "none":
                out["status"] = "ready"
        return out

    def _course_docs(self, sid: str):
        """Bài trong lớp theo thứ tự học: thứ tự kéo thả nếu đã sắp xếp, chưa thì theo ngày thêm."""
        docs = [d for d in self.library.all() if d.subject == sid]
        return docs if self.library.manual_order else sorted(docs, key=lambda d: d.created)

    def course_status(self, sid: str) -> dict:
        sub = self.library.get_subject(sid)
        docs = self._course_docs(sid)
        maps = {d.id: _mtime(self.library.doc_map_path(d.id)) for d in docs}
        out = {**self._job(f"subject:{sid}"), "title": sub.name,
               "lessons": [{"id": d.id, "title": d.title, "map": maps[d.id] is not None} for d in docs]}
        md_path, meta_path = self.library.course_map_paths(sid)
        created = _mtime(md_path)
        meta = _read_json(meta_path) if created is not None else None
        if isinstance(meta, dict):
            alive = {d.id for d in self.library.all()}
            refs = {k: v for k, v in (meta.get("refs") or {}).items() if v in alive}
            tree = parse_map(md_path.read_text(encoding="utf-8"), sub.name, refs)
            if tree:
                used = meta.get("lessons") or {}
                stale = set(used) != set(maps) or any(
                    m is None or used.get(i) is None or m > used[i] + 1e-3 for i, m in maps.items())
                ref_titles = {k: next((d.title for d in docs if d.id == v), "") for k, v in refs.items()}
                out.update(tree=tree, created=created, stale=stale,
                           refs=[{"ref": k, "id": v, "title": ref_titles[k]} for k, v in refs.items()])
                if out["status"] == "none":
                    out["status"] = "ready"
        return out

    # ---------- Chạy nền ----------

    def start_doc(self, doc_id: str) -> dict:
        self.library.get(doc_id)
        self._start(f"doc:{doc_id}", lambda: self._gen_doc(doc_id))
        return self.doc_status(doc_id)

    def start_course(self, sid: str) -> dict:
        self.library.get_subject(sid)
        if not self._course_docs(sid):
            raise MapError("Lớp chưa có bài nào để vẽ sơ đồ")
        self._start(f"subject:{sid}", lambda: self._gen_course(sid))
        return self.course_status(sid)

    def _start(self, key: str, work: Callable[[], Awaitable[None]]) -> asyncio.Task[None]:
        if (task := self._tasks.get(key)) is not None:
            return task   # đang tạo: không chạy hai lượt cùng lúc
        self._errors.pop(key, None)
        task = self._tasks[key] = asyncio.create_task(self._run(key, work))
        return task

    async def _run(self, key: str, work: Callable[[], Awaitable[None]]) -> None:
        started = time.monotonic()
        try:
            await work()
            log.info("sơ đồ %s: xong sau %.0fs", key, time.monotonic() - started)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # lỗi gì cũng báo lên web, không làm chết server
            log.warning("sơ đồ %s lỗi: %s", key, e)
            self._errors[key] = str(e) or type(e).__name__
        finally:
            self._tasks.pop(key, None)
            self._progress.pop(key, None)

    async def wait(self) -> None:
        """Chờ mọi lượt đang chạy xong (test)."""
        while self._tasks:
            await asyncio.wait(list(self._tasks.values()))

    async def _gen_doc(self, doc_id: str) -> None:
        doc = self.library.get(doc_id)
        msg = [f'Bài: "{doc.title}"']
        if doc.parts:
            parts = sorted(doc.parts.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)
            msg.append("Các phần Mimi đã chia khi giảng:\n" + "\n".join(f"{k}. {t}" for k, t in parts))
        if qs := _questions(self.library.history(doc_id)):
            msg.append("Những câu người học đã hỏi khi học bài này (chỗ chưa hiểu, cần làm rõ trong sơ đồ):\n"
                       + "\n".join(f"- {q}" for q in qs))
        msg.append("Tài liệu (toàn văn):\n\n" + self.library.text(doc_id))
        out = await self.mapper(load_prompt(self.lesson_prompt), "\n\n".join(msg))
        if parse_map(out, doc.title) is None:
            raise MapError("Claude trả về không đúng dạng sơ đồ, thử tạo lại")
        _write(self.library.doc_map_path(doc_id), out.strip() + "\n")

    async def _gen_course(self, sid: str) -> None:
        key = f"subject:{sid}"
        docs = self._course_docs(sid)
        if not docs:
            raise MapError("Lớp chưa có bài nào để vẽ sơ đồ")
        missing = [d for d in docs if _mtime(self.library.doc_map_path(d.id)) is None]
        if missing:
            # Bài thiếu sơ đồ tạo song song (bridge giới hạn MAP_SLOTS lượt cùng lúc); bài lỗi không chặn bài khác,
            # sơ đồ bài nào xong thì giữ lại, tạo lại lớp chỉ làm các bài còn thiếu
            tasks = [self._start(f"doc:{d.id}", lambda d=d: self._gen_doc(d.id)) for d in missing]
            done = 0
            self._progress[key] = f"Đang tạo sơ đồ {len(missing)} bài còn thiếu (0/{len(missing)})…"
            for t in asyncio.as_completed(tasks):
                await t
                done += 1
                self._progress[key] = f"Đang tạo sơ đồ {len(missing)} bài còn thiếu ({done}/{len(missing)})…"
            failed = [d for d in missing if _mtime(self.library.doc_map_path(d.id)) is None]
            if failed:
                d = failed[0]
                err = self._errors.get(f"doc:{d.id}", "lỗi không rõ")
                more = f" (và {len(failed) - 1} bài khác)" if len(failed) > 1 else ""
                raise MapError(f'Không tạo được sơ đồ bài "{d.title}"{more}: {err}')
        self._progress[key] = "Đang gộp sơ đồ các bài thành sơ đồ cả lớp…"
        sub = self.library.get_subject(sid)
        refs: dict[str, str] = {}
        used: dict[str, float] = {}
        blocks: list[str] = []
        for i, d in enumerate(docs, 1):
            path = self.library.doc_map_path(d.id)
            refs[f"B{i}"], used[d.id] = d.id, _mtime(path) or 0
            blocks.append(f"===== B{i}: {d.title} =====\n{path.read_text(encoding='utf-8').strip()}")
        msg = (f'Lớp: "{sub.name}" ({len(docs)} bài, đánh số theo thứ tự học)\n\n' + "\n\n".join(blocks))
        out = await self.mapper(load_prompt(self.course_prompt), msg)
        if parse_map(out, sub.name, refs) is None:
            raise MapError("Claude trả về không đúng dạng sơ đồ, thử tạo lại")
        md_path, meta_path = self.library.course_map_paths(sid)
        _write(meta_path, json.dumps({"refs": refs, "lessons": used}, ensure_ascii=False, indent=1))
        _write(md_path, out.strip() + "\n")

