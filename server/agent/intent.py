"""Tách câu nói thành việc giao cho VS Code: (dự án, yêu cầu).

"Mimi, mở dự án job hay rồi thêm phân trang cho trang danh sách" → dự án job-hay,
yêu cầu "thêm phân trang cho trang danh sách".

Chỉ nhận câu có từ khoá rõ ràng ("dự án", "project", "vs code", "mở code"); câu hỏi kiến thức như
"dự án là gì", "làm sao mở project trong vs code" vẫn là câu hỏi cho Claude (trả None), giống cách
`server/assistant/commands.py` loại câu hỏi. Tên dự án nằm ngay sau từ khoá, nhưng giữa tên và yêu cầu
thường không có dấu tách ("mở dự án job hay thêm phân trang"), nên thử các tiền tố từ dài đến ngắn với
`ProjectRegistry.match` và lấy tiền tố khớp tốt nhất. So khớp trên bản fold, cắt trên chuỗi gốc để yêu
cầu giữ nguyên chữ hoa và dấu.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from server.assistant.wake import WakeMatcher, fold

from .projects import Project, ProjectRegistry, name_key

_WORD = re.compile(r"\w+")
# Dấu tách rõ giữa tên dự án và yêu cầu ("AI assistant, sửa lỗi..."); "." chỉ tính khi sau nó là khoảng trắng
_BOUNDARY = re.compile(r"[,;:!?–—]|\.\s")

_MAX_NAME_WORDS = 6        # tên dự án nói ra dài tối đa chừng này từ
_GUESS_NAME_WORDS = 4      # không khớp dự án nào: đoán tên dài tối đa chừng này từ
CERTAIN_SCORE = 0.75       # điểm tối thiểu để chọn luôn một dự án
CERTAIN_GAP = 0.1          # ... và phải cách ứng viên thứ hai ít nhất chừng này
MAX_CANDIDATES = 3

_DEFAULT_WAKE = WakeMatcher("Mimi", ["mi mi"])


def _p(*phrases: str) -> tuple[tuple[str, ...], ...]:
    """Cụm từ → tuple từ đã fold, cụm dài trước để so khớp tham lam."""
    out = {tuple(fold(w) for w in _WORD.findall(p)) for p in phrases}
    return tuple(sorted(out - {()}, key=len, reverse=True))


_KW_PROJECT = _p("dự án", "project", "projects")
_KW_VSCODE = _p("vs code", "vscode", "visual studio code", "mở code")
# Câu hỏi kiến thức: các cụm này đứng trước từ khoá ("làm sao mở project", "cách tạo dự án .NET mới")
_Q_BEFORE = _p("làm sao", "làm thế nào", "thế nào", "như thế nào", "cách", "tại sao", "vì sao", "giải thích",
               "hướng dẫn", "khi nào", "ở đâu", "có nên", "là gì", "how", "what", "why", "when", "where",
               "which", "explain")
# ... hoặc ngay sau từ khoá là định nghĩa ("dự án là gì", "project is ...")
_Q_DEF = _p("là", "nghĩa", "is", "are", "means", "mean", "meaning")
# ... hoặc cuối câu là đuôi câu hỏi (chỉ xét khi không khớp dự án nào)
_Q_TAIL = _p("là gì", "là sao", "là ai", "nghĩa là gì", "thế nào", "ra sao", "what", "mean", "means")
# Từ đứng giữa từ khoá và tên: "mở vs code dự án X", "dự án tên là X", "open the project X"
_LEAD = _p(*(" ".join(k) for k in _KW_PROJECT + _KW_VSCODE),
           "mở", "open", "lên", "ra", "của", "tên", "tên là", "là", "the", "my", "cái", "vào", "sang", "qua",
           "tới", "in", "trong", "rồi", "và", "and", "then", "xong", "bằng", "with", "on")
# Từ nối đầu yêu cầu: "job hay rồi thêm...", "fruit rush and fix..."
_JOIN = _p("rồi", "và", "xong", "xong rồi", "sau đó", "rồi sau đó", "để", "and", "then", "and then", "thì",
           "với", "mà", "nhé", "nha", "lên", "ra", "luôn",
           "trong vs code", "bằng vs code", "in vs code", "with vs code", "vs code", "vscode",
           "trong vscode", "bằng vscode", "in vscode", "visual studio code")
# Yêu cầu chỉ toàn từ đệm thì coi như chỉ mở dự án
_FILLER = {w for p in _p("nhé", "nha", "đi", "à", "giúp", "mình", "với", "please", "nè", "thôi", "luôn", "cái",
                         "ạ", "giùm", "hộ") for w in p}
# Phần trước từ khoá: từ đệm/động từ mở ở đầu và giới từ ở cuối, phần còn lại (nếu có) là yêu cầu
# ("thêm phân trang cho dự án job hay")
_PRE_LEAD = _p("giúp mình", "giúp tôi", "giúp em", "giùm mình", "giúp", "mình", "tôi", "em", "hãy", "bạn",
               "có thể", "can you", "please", "ok", "oke", "okay", "nào", "mở", "mở giúp", "open", "bật",
               "khởi động", "launch", "start", "chuyển sang", "chuyển qua", "sang", "qua", "switch to", "go to",
               "vào", "làm việc với", "làm việc trong", "làm việc ở", "work on", "trong", "ở", "in", "on",
               "cho", "đi", "nhé", "the", "my", "lên", "ra")
_PRE_TAIL = _p("cho", "trong", "ở", "của", "in", "for", "on", "to", "of", "tại", "bên", "với", "with", "mở",
               "open", "nhé", "đi", "giúp", "mình", "giúp mình", "bằng", "vào", "into")
# Không khớp dự án nào: chỉ coi là giao việc khi trước từ khoá có động từ mở/chuyển ("mở dự án xyz"),
# không thì là câu hỏi thường ("dự án microservice có những phần nào") → hỏi Claude
_OPEN = _p("mở", "open", "bật", "khởi động", "launch", "start", "chuyển", "switch", "go to", "vào",
           "làm việc", "work on")
# Không khớp dự án nào: tên đoán được dừng trước động từ đầu yêu cầu ("dự án xyz abc làm gì đó")
_VERBS = {w for p in _p("thêm", "sửa", "viết", "làm", "tạo", "xoá", "xóa", "chạy", "kiểm", "fix", "add",
                        "write", "run", "check", "review", "refactor", "update", "cập", "đọc", "xem", "giải",
                        "build", "deploy", "test", "implement", "đổi", "tìm", "debug", "tối", "triển", "bỏ",
                        "remove", "delete", "create", "make", "rename", "optimize") for w in p}


@dataclass
class DevTask:
    project: Project | None       # dự án chắc chắn (điểm cao, cách xa ứng viên thứ hai), None nếu không chắc
    candidates: list[Project]     # ứng viên tốt nhất (≤3, dự án chắc chắn đứng đầu); [] nếu không có gì giống
    project_query: str            # phần tên dự án nghe được, vd "job hay"
    request: str                  # yêu cầu cho Claude; "" = chỉ mở dự án
    raw: str


# ---------- So cụm từ trên danh sách từ đã fold ----------

def _at(fw: list[str], i: int, phrases: tuple[tuple[str, ...], ...]) -> int:
    """Độ dài cụm (số từ) khớp tại vị trí i, 0 nếu không cụm nào khớp."""
    for p in phrases:
        if tuple(fw[i:i + len(p)]) == p:
            return len(p)
    return 0


def _ends_with(fw: list[str], lo: int, hi: int, phrases: tuple[tuple[str, ...], ...]) -> int:
    for p in phrases:
        if hi - lo >= len(p) and tuple(fw[hi - len(p):hi]) == p:
            return len(p)
    return 0


def _contains(fw: list[str], lo: int, hi: int, phrases: tuple[tuple[str, ...], ...]) -> bool:
    return any(_at(fw[:hi], i, phrases) for i in range(lo, hi))


def _skip(fw: list[str], i: int, hi: int, phrases: tuple[tuple[str, ...], ...]) -> int:
    """Bỏ liên tiếp các cụm trong phrases từ vị trí i, trả về vị trí đầu tiên không bỏ được."""
    while i < hi:
        n = _at(fw[:hi], i, phrases)
        if not n:
            break
        i += n
    return i


def _trim(fw: list[str], lo: int, hi: int) -> tuple[int, int]:
    """Cắt từ đệm đầu và giới từ cuối của phần trước từ khoá."""
    lo = _skip(fw, lo, hi, _PRE_LEAD)
    while hi > lo:
        n = _ends_with(fw, lo, hi, _PRE_TAIL)
        if not n:
            break
        hi -= n
    return lo, hi


def _find_keyword(fw: list[str]) -> tuple[int, int, str] | None:
    """(vị trí, số từ, loại "project"/"vscode") của từ khoá xuất hiện sớm nhất."""
    for i in range(len(fw)):
        for kind, phrases in (("project", _KW_PROJECT), ("vscode", _KW_VSCODE)):
            n = _at(fw, i, phrases)
            if n:
                return i, n, kind
    return None


# ---------- Tách câu ----------

class _Parser:
    def __init__(self, body: str, registry: ProjectRegistry):
        self.body = body
        self.registry = registry
        self.words = list(_WORD.finditer(body))
        self.fw = [fold(w.group()) for w in self.words]

    def text(self, lo: int, hi: int) -> str:
        """Chuỗi gốc từ words[lo] đến hết words[hi-1]."""
        if lo >= hi:
            return ""
        return self.body[self.words[lo].start():self.words[hi - 1].end()]

    def tail(self, i: int) -> str:
        return self.body[self.words[i].start():].strip() if i < len(self.words) else ""

    def boundary_after(self, k: int) -> bool:
        if k + 1 >= len(self.words):
            return False
        return bool(_BOUNDARY.search(self.body[self.words[k].end():self.words[k + 1].start()]))

    def limit(self, lo: int, hi: int, max_words: int) -> int:
        """Chỉ số hết tên có thể (không vượt dấu tách, không quá max_words từ)."""
        end = lo
        while end < hi and end - lo < max_words:
            end += 1
            if self.boundary_after(end - 1):
                break
        return end

    def find_name(self, lo: int, hi: int) -> tuple[int, list[tuple[Project, float]]] | None:
        """Tiền tố words[lo:end] khớp dự án tốt nhất, tiền tố dài hơn thắng khi hoà điểm.

        Điểm của registry chia cho độ dài chuỗi dài hơn, nên khi tên dự án dài hơn câu nghe được, thêm
        một từ của yêu cầu vào tiền tố có thể không làm giảm điểm ("management là" và "management" cùng
        0.83 với "ai management"). Khi hoà thì so tiếp bằng ratio() của difflib, có trừ phần thừa của tiền tố.
        """
        best: tuple[tuple[float, float], int, list[tuple[Project, float]]] | None = None
        for end in range(self.limit(lo, hi, _MAX_NAME_WORDS), lo, -1):
            query = self.text(lo, end)
            matches = self.registry.match(query)
            if not matches:
                continue
            rank = (matches[0][1], round(_ratio(query, matches[0][0]), 3))
            if best is None or rank > best[0]:
                best = (rank, end, matches)
        return None if best is None else (best[1], best[2])

    def guess_name_end(self, lo: int) -> int:
        """Không khớp dự án nào: tên kéo tới trước động từ đầu yêu cầu hoặc dấu tách."""
        end = lo
        hi = self.limit(lo, len(self.words), _GUESS_NAME_WORDS)
        while end < hi and self.fw[end] not in _VERBS:
            end += 1
        return end

    def request_from(self, i: int) -> str:
        """Yêu cầu từ vị trí i: bỏ từ nối đầu câu, bỏ dấu câu cuối; chỉ còn từ đệm thì là ""."""
        i = _skip(self.fw, i, len(self.fw), _JOIN)
        if all(w in _FILLER for w in self.fw[i:]):
            return ""
        return self.tail(i).rstrip(" .,;:")


def _ratio(query: str, project: Project) -> float:
    """ratio() của difflib (2M/(a+b)) giữa câu nghe được và tên/alias gần nhất của dự án."""
    q = name_key(query)
    forms = [project.name, project.name.replace("/", " "), project.path.name, *project.aliases]
    return max(SequenceMatcher(None, q, k).ratio() for k in (name_key(f) for f in forms) if k)


def pick_project(matches: list[tuple[Project, float]]) -> tuple[Project | None, list[Project]]:
    top = [p for p, _ in matches[:MAX_CANDIDATES]]
    if not matches:
        return None, []
    gap = matches[0][1] - (matches[1][1] if len(matches) > 1 else 0.0)
    certain = matches[0][1] >= CERTAIN_SCORE and gap >= CERTAIN_GAP
    return (matches[0][0] if certain else None), top


def _join(pre: str, post: str) -> str:
    return f"{pre}, {post}" if pre and post else pre or post


LOOSE_SCORE = 0.85         # tab Công việc, câu không có từ khoá: tên dự án phải khớp chừng này
LOOSE_MIN_KEY = 6          # ... và đủ dài (tên một từ ngắn như "dang" dễ trùng chữ thường: "đăng nhập")


def find_project_anywhere(text: str, registry: ProjectRegistry, *,
                          wake: WakeMatcher | None = None) -> DevTask | None:
    """Tab Công việc: tìm tên dự án ở bất kỳ đâu trong câu, kể cả khi STT nghe sai từ khoá
    ("Mở dạng job hay" thay cho "mở dự án job hay"). Chỉ nhận khi chắc chắn, không thì None.

    Phần trước tên chỉ là yêu cầu khi có động từ ("thêm phân trang cho job hay"); không thì là từ đệm
    hoặc từ khoá bị nghe sai ("mở dạng") và bị bỏ.
    """
    m = (wake or _DEFAULT_WAKE).match(text)
    ps = _Parser(m.rest if m.found else text, registry)
    fw = ps.fw
    best: tuple[float, int, int, list[tuple[Project, float]]] | None = None
    for lo in range(len(fw)):
        found = ps.find_name(lo, len(fw))
        if found is None:
            continue
        end, matches = found
        score = matches[0][1]
        if score >= LOOSE_SCORE and len(name_key(ps.text(lo, end))) >= LOOSE_MIN_KEY \
                and (best is None or score > best[0]):
            best = (score, lo, end, matches)
    if best is None:
        return None
    _, lo, end, matches = best
    project, candidates = pick_project(matches)
    if project is None:
        return None
    pre_lo, pre_hi = _trim(fw, 0, lo)
    pre = ps.text(pre_lo, pre_hi) if any(w in _VERBS for w in fw[pre_lo:pre_hi]) else ""
    return DevTask(project, candidates, ps.text(lo, end), _join(pre, ps.request_from(end)), text)


OPEN_MAX_WORDS = 4         # "mở X": X dài tối đa chừng này từ mới coi là tên dự án (dài hơn là việc)
# Đứng trước động từ mở: "giúp mình mở...", "bạn mở..."
_OPEN_LEAD = _p("giúp mình", "giúp tôi", "giúp em", "giúp", "mình", "tôi", "em", "hãy", "bạn", "có thể",
                "can you", "please", "ok", "oke", "okay", "nào")


def guess_open(text: str, registry: ProjectRegistry, *, wake: WakeMatcher | None = None) -> DevTask | None:
    """Tab Công việc: câu ngắn "mở X" ("mở dạng karaoke", "mở diễn") mà X không khớp chắc dự án nào.

    Trả DevTask(project=None, candidates=ứng viên gần giống) để Mimi hỏi lại, không để server đem câu này
    giao cho dự án đang làm. None nếu câu không có dạng đó. X bỏ được một từ đầu (từ khoá "dự án" bị nghe
    sai thành "dạng"). Câu có động từ việc ("mở file README rồi sửa...") vẫn là việc, trả None.
    """
    m = (wake or _DEFAULT_WAKE).match(text)
    ps = _Parser(m.rest if m.found else text, registry)
    fw = ps.fw
    i = _skip(fw, 0, len(fw), _OPEN_LEAD)
    n = _at(fw, i, _OPEN)
    if not n:
        return None
    j = _skip(fw, i + n, len(fw), _LEAD)
    end = j
    while end < len(fw) and fw[end] not in _FILLER:
        end += 1
    if j >= end or end - j > OPEN_MAX_WORDS or any(w in _VERBS for w in fw[j:end]):
        return None
    best: list[tuple[Project, float]] = []
    for lo in range(j, min(j + 2, end)):
        found = registry.similar(ps.text(lo, end))
        if found and (not best or found[0][1] > best[0][1]):
            best = found
    return DevTask(None, [p for p, _ in best[:MAX_CANDIDATES]], ps.text(j, end), "", text)


def parse_dev_task(text: str, registry: ProjectRegistry, *, wake: WakeMatcher | None = None) -> DevTask | None:
    """Câu nói → DevTask, hoặc None nếu không phải việc giao cho VS Code (để server hỏi Claude như thường).

    `wake`: bộ nhận tên trợ lý để bỏ tên còn sót đầu câu ("Mimi ơi mở dự án..."), mặc định "Mimi".
    """
    raw = text
    m = (wake or _DEFAULT_WAKE).match(text)
    body = m.rest if m.found else text
    ps = _Parser(body, registry)
    fw = ps.fw
    kw = _find_keyword(fw)
    if kw is None:
        return None
    i, n, kind = kw
    if _contains(fw, 0, i, _Q_BEFORE):
        return None                          # "làm sao mở project trong vs code"
    if _at(fw, i + n, _Q_DEF):
        return None                          # "dự án là gì"
    has_project_kw = kind == "project" or _contains(fw, i + n, len(fw), _KW_PROJECT)

    pre_lo, pre_hi = _trim(fw, 0, i)
    pre = ps.text(pre_lo, pre_hi)            # yêu cầu nói trước từ khoá ("thêm phân trang cho dự án X")
    j = _skip(fw, i + n, len(fw), _LEAD)

    found = ps.find_name(j, len(fw)) if j < len(fw) else None
    if found is None and kind == "vscode" and pre_lo < pre_hi:
        # "mở job hay bằng vs code": tên đứng trước từ khoá
        before = ps.find_name(pre_lo, pre_hi)
        if before is not None:
            end, matches = before
            project, candidates = pick_project(matches)
            request = _join(ps.text(*_trim(fw, end, pre_hi)), ps.request_from(j))
            return DevTask(project, candidates, ps.text(pre_lo, end), request, raw)

    if found is not None:
        end, matches = found
        project, candidates = pick_project(matches)
        request = ps.request_from(end)
        # "project management là gì": khớp nhầm một dự án, phần còn lại chỉ là đuôi câu hỏi
        rest = fw[_skip(fw, end, len(fw), _JOIN):]
        if len(rest) <= 3 and _at(rest, 0, _Q_DEF):
            return None
        return DevTask(project, candidates, ps.text(j, end), _join(pre, request), raw)

    # Không khớp dự án nào
    if body.rstrip().endswith("?") or _ends_with(fw, 0, len(fw), _Q_TAIL):
        return None                          # "project management là gì"
    if not has_project_kw and j < len(fw):
        return None                          # "vs code có extension nào cho python": câu hỏi thường
    if not _contains(fw, 0, i, _OPEN):
        return None                          # "dự án microservice thường có những phần nào": câu hỏi thường
    end = ps.guess_name_end(j)
    return DevTask(None, [], ps.text(j, end), _join(pre, ps.request_from(end)), raw)
