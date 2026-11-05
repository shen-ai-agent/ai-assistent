"""Thư viện tài liệu học (tab Học, T-012): mỗi tài liệu một thư mục trong [study] dir (_local/study).

  <id>/doc.md        nội dung đã chuyển thành text; Claude đọc file này bằng tool Read (cwd = thư mục tài liệu)
  <id>/meta.json     tiêu đề, tiến độ (phần đang học / tổng số phần, tên các phần), session Claude
  <id>/history.jsonl các lượt đã giảng xong: câu bạn hỏi + câu Mimi trả lời (đã bỏ dòng tiến độ)

Tiến độ lấy từ dòng `[Phần k/N: Tên phần]` Claude viết đầu mỗi lượt giảng (prompts/study_reply.md):
MarkerFilter cắt dòng đó khỏi stream để không đọc thành tiếng.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

TEXT_EXT = {".txt", ".md", ".markdown"}
DOCX_EXT = {".docx"}
MAX_LINE = 1000   # tool Read cắt dòng dài hơn 2000 ký tự: dòng dán vào quá dài thì ngắt ở khoảng trắng
_ID = re.compile(r"^[0-9]{8}-[0-9]{6}(-[0-9]+)?$")


class StudyError(ValueError):
    """Lỗi đọc được cho người dùng (file không hỗ trợ, rỗng, quá dài, không có tài liệu)."""


@dataclass
class StudyDoc:
    id: str
    title: str
    source: str                   # "paste" hoặc tên file đã tải lên
    chars: int
    created: float
    updated: float
    session_id: str | None = None   # session Claude của tài liệu này (None = chưa giảng lượt nào)
    part: int = 0                    # phần đang giảng (0 = chưa bắt đầu)
    total: int = 0                   # tổng số phần Claude chia (0 = chưa biết)
    parts: dict[str, str] = field(default_factory=dict)   # "k" → tên phần

    def public(self) -> dict:
        d = asdict(self)
        d.pop("session_id")
        d["started"] = self.session_id is not None
        return d


# ---------- Chuyển file thành text ----------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_text(data: bytes) -> str:
    """Text của file .docx: mỗi đoạn một dòng, tiêu đề thành `#`, danh sách thành `-`, bảng thành bảng markdown `| a | b |`.
    Chỉ dùng thư viện chuẩn (docx là zip chứa word/document.xml)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            root = ET.fromstring(z.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise StudyError("Không đọc được file .docx (file hỏng hoặc không phải Word)") from e
    body = root.find(f"{_W}body")
    lines: list[str] = []
    for el in body if body is not None else []:
        if el.tag == f"{_W}p":
            lines.append(_paragraph(el))
        elif el.tag == f"{_W}tbl":
            # Bảng markdown (dòng đầu là tiêu đề) để tab Học bài hiện thành bảng; | trong ô đổi thành /
            for i, row in enumerate(el.iter(f"{_W}tr")):
                cells = [" ".join(_paragraph(p) for p in tc.iter(f"{_W}p")).strip().replace("|", "/")
                         for tc in row.iter(f"{_W}tc")]
                lines.append(f"| {' | '.join(cells)} |")
                if i == 0:
                    lines.append(f"|{'---|' * len(cells)}")
            lines.append("")
    return "\n".join(lines)


def _paragraph(p) -> str:
    parts = []
    for node in p.iter():
        if node.tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
    text = "".join(parts).strip()
    style = p.find(f"{_W}pPr/{_W}pStyle")
    name = (style.get(f"{_W}val") or "") if style is not None else ""
    if text and (m := re.match(r"(?i)heading\s*(\d)", name)):
        return "#" * min(int(m.group(1)), 6) + " " + text
    if text and (name.lower().startswith("list") or p.find(f"{_W}pPr/{_W}numPr") is not None):
        return "- " + text
    return text


def extract_text(filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext in TEXT_EXT:
        for enc in ("utf-8-sig", "utf-16", "cp1258"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        raise StudyError("Không đọc được bảng mã của file text (lưu lại bằng UTF-8)")
    if ext in DOCX_EXT:
        return docx_text(data)
    raise StudyError(f"Chưa hỗ trợ file {ext or filename}: dùng .txt, .md hoặc .docx")


def clean_text(text: str) -> str:
    """Chuẩn hoá xuống dòng, bỏ dòng trống thừa, ngắt dòng quá dài (Read cắt dòng > 2000 ký tự)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    out: list[str] = []
    for line in text.split("\n"):
        line = line.rstrip()
        while len(line) > MAX_LINE:
            cut = line.rfind(" ", MAX_LINE // 2, MAX_LINE)
            cut = cut if cut > 0 else MAX_LINE
            out.append(line[:cut])
            line = line[cut:].lstrip()
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def guess_title(text: str, source: str) -> str:
    if source != "paste":
        return Path(source).stem[:120]
    for line in text.split("\n"):
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:80] + ("…" if len(line) > 80 else "")
    return "Tài liệu"


# ---------- Dòng tiến độ "[Phần k/N: Tên phần]" ----------

MARKER = re.compile(r"\[\s*Phần\s+(\d+)\s*/\s*(\d+)\s*(?:[:：\-–—]\s*([^\]\n]*?))?\s*\][ \t]*\n?", re.IGNORECASE)
_MARKER_MAX = 200   # chưa thấy "]" sau chừng này ký tự thì không phải dòng tiến độ


class MarkerFilter:
    """Cắt dòng tiến độ ở đầu câu trả lời. push(delta) trả phần text được đi tiếp (có thể rỗng khi đang chờ
    đủ chữ để biết có phải dòng tiến độ không); found = (k, N, tên) khi vừa cắt được."""

    def __init__(self):
        self.buf = ""
        self.done = False
        self.found: tuple[int, int, str] | None = None

    def push(self, text: str) -> str:
        if self.done:
            return text
        self.buf += text
        head = self.buf.lstrip()
        if not head:
            return ""
        if not head.startswith("["):
            return self._release(self.buf)
        if m := MARKER.match(head):
            self.found = (int(m.group(1)), int(m.group(2)), (m.group(3) or "").strip())
            return self._release(head[m.end():].lstrip("\n"))
        if "]" in head or "\n" in head or len(head) > _MARKER_MAX:
            return self._release(self.buf)
        return ""

    def flush(self) -> str:
        return "" if self.done else self._release(self.buf)

    def _release(self, text: str) -> str:
        self.done, self.buf = True, ""
        return text


# ---------- Thư viện ----------

class Library:
    def __init__(self, root: Path, max_chars: int = 200_000):
        self.root = root
        self.max_chars = max_chars

    def _dir(self, doc_id: str) -> Path:
        if not isinstance(doc_id, str) or not _ID.match(doc_id):
            raise StudyError("Không có tài liệu này")
        return self.root / doc_id

    def create(self, text: str, title: str = "", source: str = "paste") -> StudyDoc:
        text = clean_text(text)
        if len(text.strip()) < 20:
            raise StudyError("Tài liệu trống hoặc quá ngắn")
        if len(text) > self.max_chars:
            raise StudyError(f"Tài liệu dài {len(text):,} ký tự, quá giới hạn {self.max_chars:,}. "
                             "Chia nhỏ thành vài tài liệu (mỗi chương một tài liệu).")
        self.root.mkdir(parents=True, exist_ok=True)
        base = time.strftime("%Y%m%d-%H%M%S")
        doc_id, n = base, 1
        while (self.root / doc_id).exists():
            n += 1
            doc_id = f"{base}-{n}"
        now = time.time()
        doc = StudyDoc(doc_id, title.strip()[:120] or guess_title(text, source), source, len(text), now, now)
        d = self.root / doc_id
        d.mkdir()
        (d / "doc.md").write_text(text, encoding="utf-8")
        self.save(doc, touch=False)
        return doc

    def get(self, doc_id: str) -> StudyDoc:
        path = self._dir(doc_id) / "meta.json"
        if not path.exists():
            raise StudyError("Không có tài liệu này")
        return StudyDoc(**json.loads(path.read_text(encoding="utf-8")))

    def all(self) -> list[StudyDoc]:
        """Mọi tài liệu, học gần nhất trước."""
        if not self.root.exists():
            return []
        docs = []
        for d in self.root.iterdir():
            if d.is_dir() and _ID.match(d.name) and (d / "meta.json").exists():
                try:
                    docs.append(self.get(d.name))
                except (ValueError, TypeError, StudyError):
                    continue  # meta hỏng: bỏ qua, không làm hỏng cả danh sách
        return sorted(docs, key=lambda x: x.updated, reverse=True)

    def save(self, doc: StudyDoc, touch: bool = True) -> None:
        if touch:
            doc.updated = time.time()
        path = self._dir(doc.id) / "meta.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(doc), ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    def workdir(self, doc_id: str) -> Path:
        return self._dir(doc_id)

    def text(self, doc_id: str) -> str:
        return (self._dir(doc_id) / "doc.md").read_text(encoding="utf-8")

    def history(self, doc_id: str) -> list[dict]:
        path = self._dir(doc_id) / "history.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def append_history(self, doc_id: str, question: str, answer: str, part: int) -> None:
        rec = {"at": time.time(), "question": question, "answer": answer, "part": part}
        with (self._dir(doc_id) / "history.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def set_part(self, doc: StudyDoc, part: int, total: int, title: str) -> None:
        doc.part, doc.total = part, max(total, part)
        if title:
            doc.parts[str(part)] = title
        self.save(doc)

    def reset(self, doc_id: str) -> StudyDoc:
        """Học lại từ đầu: session Claude mới, xoá tiến độ và lịch sử (tài liệu giữ nguyên)."""
        doc = self.get(doc_id)
        doc.session_id, doc.part, doc.total, doc.parts = None, 0, 0, {}
        (self._dir(doc_id) / "history.jsonl").unlink(missing_ok=True)
        self.save(doc)
        return doc

    def rename(self, doc_id: str, title: str) -> StudyDoc:
        doc = self.get(doc_id)
        if title.strip():
            doc.title = title.strip()[:120]
            self.save(doc, touch=False)
        return doc

    def delete(self, doc_id: str) -> None:
        d = self._dir(doc_id)
        if not (d / "meta.json").exists():
            raise StudyError("Không có tài liệu này")
        shutil.rmtree(d)
