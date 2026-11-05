"""Danh bạ dự án trên máy: quét thư mục gốc, mô tả, stack, lần dùng Claude gần nhất, so tên nói ra.

Mỗi thư mục con trực tiếp của một root là một dự án. Thư mục không có dấu hiệu dự án (.git, README,
package.json...) nhưng chứa thư mục con có dấu hiệu là thư mục nhóm (vd `D:/projects/Shen`): lấy các
thư mục con đó, tên hiển thị "Shen/agentic-comics". Chỉ đọc hai tầng, không đi đệ quy cả cây, nên gọi
`refresh()` lại khi cần cũng rẻ.

Tên nói ra qua Whisper lệch so với tên thư mục ("AI assistant" cho `AI-assistent`, "scan order enjoy"
cho `ScanOrderEnjoy`), nên so trên chuỗi đã `fold` (bỏ dấu, gộp chữ lặp) và bỏ hết khoảng trắng, chấm
gần đúng như `WakeMatcher`.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from server.assistant.wake import _similarity, fold

log = logging.getLogger(__name__)

# Thư mục không bao giờ là dự án
_IGNORED = {"node_modules", "__pycache__", "_local", "venv", "env", "dist", "build", "bin", "obj", "packages",
            "system volume information"}
# Tên file/thư mục cho biết đây là một dự án (so không phân biệt hoa thường)
_MARKERS = {".git", ".claude", ".vscode", "readme.md", "readme", "readme.txt", "claude.md", "agents.md",
            "package.json", "pyproject.toml", "requirements.txt", "setup.py", "pipfile", "go.mod", "cargo.toml",
            "pom.xml", "build.gradle", "build.gradle.kts", "composer.json", "gemfile", "pubspec.yaml",
            "deno.json", "dockerfile", "docker-compose.yml", "makefile", "cmakelists.txt", "projectsettings"}
_MARKER_SUFFIXES = (".sln", ".csproj", ".fsproj", ".vbproj", ".uproject")
# Stack theo file đánh dấu, thứ tự trong danh sách là thứ tự trả về
_STACKS: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = [
    # (stack, tên file, đuôi file)
    ("python", ("pyproject.toml", "requirements.txt", "setup.py", "pipfile"), ()),
    ("node", ("package.json",), ()),
    ("dotnet", (), (".sln", ".csproj", ".fsproj", ".vbproj")),
    ("unity", (), (".unity",)),   # ngoài ra: có cả Assets/ và ProjectSettings/
    ("go", ("go.mod",), ()),
    ("rust", ("cargo.toml",), ()),
    ("java", ("pom.xml", "build.gradle", "build.gradle.kts"), ()),
    ("flutter", ("pubspec.yaml",), ()),
    ("php", ("composer.json",), ()),
    ("ruby", ("gemfile",), ()),
]
_MAX_CHILDREN = 40          # số thư mục con tối đa đọc thêm khi tìm stack
SIMILAR_MIN = 0.45          # ProjectRegistry.similar: điểm tối thiểu để gợi ý khi hỏi lại
_DESC_MAX = 160
_README_NAMES = ("readme.md", "readme", "readme.txt", "claude.md")

_SLUG = re.compile(r"[^A-Za-z0-9]")
_CHUNK = re.compile(r"[^\W_]+")
# Ranh giới camelCase / chữ-số: "ScanOrderEnjoy" → Scan|Order|Enjoy, "Track6" → Track|6, "APIServer" → API|Server
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")


@dataclass
class Project:
    name: str                 # tên hiển thị: tên thư mục, dự án con là "Shen/agentic-comics"
    path: Path
    aliases: list[str] = field(default_factory=list)
    description: str = ""     # dòng mô tả đầu tiên có nghĩa của README.md / CLAUDE.md, ≤160 ký tự
    stack: list[str] = field(default_factory=list)
    last_used: float | None = None   # epoch giây: mtime mới nhất của *.jsonl trong thư mục log Claude

    def to_dict(self) -> dict:
        return {"name": self.name, "path": str(self.path), "aliases": list(self.aliases),
                "description": self.description, "stack": list(self.stack), "last_used": self.last_used}


# ---------- Tách từ, so tên ----------

def name_words(text: str) -> list[str]:
    """Từ đã fold của một tên: tách camelCase, kebab, snake, chữ-số. "AI-assistent" → ["ai", "asistent"]."""
    words: list[str] = []
    for chunk in _CHUNK.findall(text):
        words.extend(fold(w) for w in _CAMEL.split(chunk) if w)
    return [w for w in words if w]


def name_key(text: str) -> str:
    """Dạng so khớp: các từ đã fold nối liền, gộp chữ lặp ở chỗ nối ("fruit time" → "fruitime")."""
    return re.sub(r"(.)\1+", r"\1", "".join(name_words(text)))


def name_score(query: str, key: str) -> float:
    """Độ giống 0..1 giữa hai chuỗi đã qua `name_key`.

    Trùng hẳn = 1. Khớp tiền tố ("figmaconsole" của "figmaconsolemcp") được tối thiểu 0.6-0.95 tuỳ độ phủ,
    luôn dưới 1 nên tên đúng thắng tên dài hơn có cùng tiền tố ("job hay" → job-hay, không phải job-hay-old).
    """
    if not query or not key:
        return 0.0
    if query == key:
        return 1.0
    score = _similarity(query, key)
    if len(query) >= 3 and key.startswith(query):
        score = max(score, 0.6 + 0.35 * len(query) / len(key))
    return score


# ---------- Log Claude Code ----------

def claude_slug(path: Path) -> str:
    """Tên thư mục log của Claude Code: mọi ký tự không phải chữ/số thành "-"."""
    return _SLUG.sub("-", str(path))


def _default_claude_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _claude_index(base: Path) -> dict[str, list[Path]]:
    """slug viết thường → các thư mục log (có thể có cả "D--..." và "d--...")."""
    index: dict[str, list[Path]] = {}
    try:
        with os.scandir(base) as it:
            for e in it:
                if e.is_dir():
                    index.setdefault(e.name.lower(), []).append(Path(e.path))
    except OSError:
        return {}
    for dirs in index.values():
        dirs.sort()
    return index


def claude_project_dirs(path: Path, base: Path) -> list[Path]:
    """Các thư mục con của base có tên == slug(path), không phân biệt hoa thường; [] nếu chưa có."""
    return list(_claude_index(base).get(claude_slug(path).lower(), []))


def _last_used(dirs: list[Path]) -> float | None:
    latest: float | None = None
    for d in dirs:
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.endswith(".jsonl") and e.is_file():
                        m = e.stat().st_mtime
                        if latest is None or m > latest:
                            latest = m
        except OSError:
            continue
    return latest


# ---------- Quét thư mục ----------

def _entries(path: Path) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as it:
            return list(it)
    except OSError:
        return []


def _ignored(name: str) -> bool:
    return name.startswith((".", "$")) or name.lower() in _IGNORED


def _has_marker(entries: list[os.DirEntry]) -> bool:
    for e in entries:
        low = e.name.lower()
        if low in _MARKERS or low.endswith(_MARKER_SUFFIXES):
            return True
    return False


def _stack_of(entries: list[os.DirEntry], found: set[str]) -> None:
    names = {e.name.lower() for e in entries}
    for stack, files, suffixes in _STACKS:
        if names.intersection(files) or any(n.endswith(suffixes) for n in names if suffixes):
            found.add(stack)
    if "assets" in names and "projectsettings" in names:
        found.add("unity")


def _detect_stack(entries: list[os.DirEntry]) -> list[str]:
    """Stack theo file đánh dấu ở thư mục dự án và các thư mục con trực tiếp (Backend/, JONE02-API/...)."""
    found: set[str] = set()
    _stack_of(entries, found)
    children = [e for e in entries if not _ignored(e.name) and e.is_dir()][:_MAX_CHILDREN]
    for child in children:
        _stack_of(_entries(Path(child.path)), found)
    return [s for s, _, _ in _STACKS if s in found]


_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_HTML = re.compile(r"<[^>]+>")
_MD_EMPH = re.compile(r"[*_`~]+")
_MD_LIST = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
_BOILERPLATE = ("provides guidance to claude code", "this file provides guidance")


def _clean_line(line: str) -> str:
    s = line.strip()
    if not s or s.startswith(("#", "|", "```", "~~~", "<!--", "---", "===", "***")):
        return ""
    if s.startswith(("![", "[![")) or (s.startswith("<") and not _MD_HTML.sub("", s).strip()):
        return ""
    s = s.lstrip("> ").strip()
    s = _MD_IMAGE.sub("", s)
    s = _MD_LINK.sub(r"\1", s)
    s = _MD_HTML.sub("", s)
    s = _MD_EMPH.sub("", s)
    s = _MD_LIST.sub("", s).strip()
    if sum(ch.isalpha() for ch in s) < 3 or any(b in s.lower() for b in _BOILERPLATE):
        return ""
    return s


def _describe(path: Path, entries: list[os.DirEntry]) -> str:
    """Dòng có nghĩa đầu tiên của README rồi CLAUDE.md: bỏ heading, badge, bảng, code, frontmatter."""
    by_name = {e.name.lower(): e for e in entries if e.is_file()}
    for wanted in _README_NAMES:
        e = by_name.get(wanted)
        if e is None:
            continue
        try:
            with open(e.path, encoding="utf-8-sig", errors="replace") as f:
                text = f.read(16384)
        except OSError:
            continue
        lines = text.splitlines()
        in_fence = False
        start = 0
        if lines and lines[0].strip() == "---":   # frontmatter YAML
            end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), 0)
            start = end + 1
        for line in lines[start:]:
            if line.strip().startswith(("```", "~~~")):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            s = _clean_line(line)
            if s:
                return s if len(s) <= _DESC_MAX else s[:_DESC_MAX - 1].rstrip() + "…"
    return ""


# ---------- Danh bạ ----------

def _norm_path(p: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


class ProjectRegistry:
    """Danh sách dự án quét từ các root + file overrides (TOML: exclude, [aliases], [extra])."""

    def __init__(self, roots: list[Path], overrides: Path | None = None, claude_dir: Path | None = None,
                 min_score: float = 0.6):
        self.roots = [Path(r) for r in roots]
        self.overrides = Path(overrides) if overrides else None
        self.claude_dir = Path(claude_dir) if claude_dir else _default_claude_dir()
        self.min_score = min_score
        self._projects: list[Project] = []
        self._keys: dict[int, list[str]] = {}   # id(project) → các dạng so khớp
        self.refresh()

    # ----- quét -----

    def _load_overrides(self) -> dict:
        if self.overrides is None or not self.overrides.is_file():
            return {}
        try:
            return tomllib.loads(self.overrides.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as e:
            log.warning("Không đọc được %s: %s", self.overrides, e)
            return {}

    def _scan_root(self, root: Path) -> list[tuple[str, Path, list[os.DirEntry]]]:
        found: list[tuple[str, Path, list[os.DirEntry]]] = []
        for e in sorted(_entries(root), key=lambda e: e.name.lower()):
            if _ignored(e.name) or not e.is_dir():
                continue
            path = Path(root) / e.name
            entries = _entries(path)
            if _has_marker(entries) or _claude_dirs_in(path, self._index):
                found.append((e.name, path, entries))
                continue
            subs = []
            for c in sorted(entries, key=lambda c: c.name.lower()):
                if _ignored(c.name) or not c.is_dir():
                    continue
                sub = path / c.name
                sub_entries = _entries(sub)
                if _has_marker(sub_entries) or _claude_dirs_in(sub, self._index):
                    subs.append((f"{e.name}/{c.name}", sub, sub_entries))
            # Thư mục nhóm thì lấy các dự án con; không có gì thì vẫn coi chính nó là một dự án
            found.extend(subs or [(e.name, path, entries)])
        return found

    def refresh(self) -> None:
        """Quét lại các root, đọc lại overrides và log Claude. Root không tồn tại thì bỏ qua."""
        cfg = self._load_overrides()
        self._index = _claude_index(self.claude_dir)
        raw: list[tuple[str, Path, list[os.DirEntry]]] = []
        for root in self.roots:
            if root.is_dir():
                raw.extend(self._scan_root(root))
        seen = {_norm_path(p): i for i, (_, p, _) in enumerate(raw)}
        extra_aliases: dict[str, list[str]] = {}
        for name, value in (cfg.get("extra") or {}).items():
            path = Path(str(value))
            key = _norm_path(path)
            if key in seen:   # đã quét được: tên trong [extra] thành alias
                extra_aliases.setdefault(key, []).append(name)
            elif path.is_dir():
                seen[key] = len(raw)
                raw.append((name, path, _entries(path)))

        def lookup(table: dict, name: str, path: Path) -> list[str]:
            """Giá trị trong bảng overrides theo tên hiển thị, tên thư mục hoặc đường dẫn."""
            out: list[str] = []
            wanted = {name.lower(), path.name.lower()}
            for k, v in table.items():
                if k.lower() in wanted or _norm_path(k) == _norm_path(path):
                    out.extend([v] if isinstance(v, str) else list(v))
            return out

        excluded = {str(x).lower() for x in cfg.get("exclude") or []}
        excluded_paths = {_norm_path(x) for x in cfg.get("exclude") or []}
        aliases_cfg = cfg.get("aliases") or {}
        projects: list[Project] = []
        keys: dict[int, list[str]] = {}
        for name, path, entries in raw:
            if name.lower() in excluded or path.name.lower() in excluded or _norm_path(path) in excluded_paths:
                continue
            aliases = lookup(aliases_cfg, name, path) + extra_aliases.get(_norm_path(path), [])
            p = Project(name=name, path=path, aliases=aliases, description=_describe(path, entries),
                        stack=_detect_stack(entries),
                        last_used=_last_used(_claude_dirs_in(path, self._index)))
            projects.append(p)
            forms = [name, path.name, *aliases]
            if "/" in name:
                forms.append(name.replace("/", " "))
            keys[id(p)] = sorted({k for k in (name_key(f) for f in forms) if k})
        projects.sort(key=lambda p: (p.last_used is None, -(p.last_used or 0.0), p.name.lower()))
        self._projects, self._keys = projects, keys

    # ----- tra cứu -----

    def all(self) -> list[Project]:
        """Mọi dự án, dùng gần nhất lên đầu (chưa dùng Claude xuống cuối), rồi theo tên."""
        return list(self._projects)

    def match(self, spoken: str) -> list[tuple[Project, float]]:
        """Dự án giống tên nói ra, điểm 0..1 giảm dần, chỉ giữ điểm ≥ min_score. Hoà điểm: dùng gần hơn trước."""
        query = name_key(spoken)
        if not query:
            return []
        out: list[tuple[Project, float]] = []
        for p in self._projects:
            score = max((name_score(query, k) for k in self._keys.get(id(p), [])), default=0.0)
            if score >= self.min_score:
                out.append((p, round(score, 3)))
        out.sort(key=lambda t: (-t[1], t[0].last_used is None, -(t[0].last_used or 0.0), t[0].name.lower()))
        return out

    def similar(self, spoken: str, min_score: float = SIMILAR_MIN) -> list[tuple[Project, float]]:
        """Như `match` nhưng lỏng hơn, chỉ để gợi ý khi hỏi lại ("ý bạn là guitar karaoke?"), không để chọn.
        Tên nghe được nằm trong tên dự án ("karaoke" của "guitar-karaoke") cũng tính, không chỉ tiền tố."""
        query = name_key(spoken)
        if not query:
            return []
        out: list[tuple[Project, float]] = []
        for p in self._projects:
            keys = self._keys.get(id(p), [])
            score = max((name_score(query, k) for k in keys), default=0.0)
            if len(query) >= 5:
                score = max([score, *(0.5 + 0.35 * len(query) / len(k) for k in keys if query in k)])
            if score >= min_score:
                out.append((p, round(score, 3)))
        out.sort(key=lambda t: (-t[1], t[0].last_used is None, -(t[0].last_used or 0.0), t[0].name.lower()))
        return out

    def get(self, name: str) -> Project | None:
        """Dự án đúng tên (không phân biệt hoa thường) hoặc đúng đường dẫn."""
        low = name.lower()
        for p in self._projects:
            if p.name.lower() == low:
                return p
        target = _norm_path(name)
        for p in self._projects:
            if _norm_path(p.path) == target:
                return p
        return None


def _claude_dirs_in(path: Path, index: dict[str, list[Path]]) -> list[Path]:
    """Như `claude_project_dirs` nhưng tra trong index đã đọc sẵn (một lần mỗi `refresh`)."""
    return list(index.get(claude_slug(path).lower(), []))
