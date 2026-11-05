"""Test danh bạ dự án và tách câu giao việc cho VS Code (server/agent/projects.py, intent.py).

Cây thư mục, file overrides và thư mục log Claude đều dựng trong tmp_path, không đọc máy thật.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from server.agent.intent import parse_dev_task
from server.agent.projects import ProjectRegistry, claude_project_dirs, claude_slug, name_key


def touch(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def log_dir(claude: Path, project: Path, mtime: float, *, swap_case: bool = False) -> Path:
    """Thư mục log Claude giả cho project, một file .jsonl có mtime cho trước."""
    slug = claude_slug(project)
    if swap_case:
        slug = slug[0].swapcase() + slug[1:]
    d = claude / slug
    f = touch(d / f"{int(mtime)}.jsonl", "{}\n")
    os.utime(f, (mtime, mtime))
    return d


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "projects"
    touch(root / "AI-assistent" / "README.md",
          "# Voice\n\n[![CI](https://x/badge.svg)](https://x)\n\nTrợ lý giọng nói **local** bọc quanh "
          "[Claude](https://claude.ai).\n")
    touch(root / "AI-assistent" / "requirements.txt", "fastapi==0.1\n")
    (root / "AI-assistent" / ".git").mkdir()
    touch(root / "ScanOrderEnjoy" / "package.json", "{}")
    touch(root / "ScanOrderEnjoy" / "CLAUDE.md",
          "# CLAUDE.md\n\nThis file provides guidance to Claude Code (claude.ai/code) when working here.\n\n"
          "## Overview\n\n" + "Ứng dụng gọi món bằng mã QR " * 10 + "\n")
    (root / "job-hay" / ".git").mkdir(parents=True)
    touch(root / "job-hay-old" / "README.md", "# Job Hay cũ\n")
    touch(root / "fruit-rush" / "README.md", "Game trái cây\n")
    touch(root / "figma-console-mcp" / "package.json", "{}")
    touch(root / "dotnet-app" / "Backend" / "Api.csproj", "<Project/>")
    touch(root / "dotnet-app" / "README.md", "API .NET\n")
    # Thư mục nhóm: không có dấu hiệu dự án, các thư mục con có
    (root / "Shen" / "agentic-comics" / ".git").mkdir(parents=True)
    touch(root / "Shen" / "ai-management" / "package.json", "{}")
    (root / "Shen" / "notes").mkdir()
    # Không dấu hiệu, con cũng không: vẫn là một dự án
    touch(root / "learning-aws" / "Slides" / "intro.pdf")
    # Không dấu hiệu nhưng có log Claude: là dự án, không tách thành dự án con
    touch(root / "Anvita" / "design" / "README.md", "Thiết kế\n")
    # Hai dự án cùng dạng so khớp "foobar", để kiểm hoà điểm
    touch(root / "foo-bar" / "README.md", "a\n")
    touch(root / "FooBar" / "README.md", "b\n")
    (root / "node_modules" / "x").mkdir(parents=True)
    (root / ".hidden" / ".git").mkdir(parents=True)

    claude = tmp_path / "claude"
    log_dir(claude, root / "AI-assistent", 3000, swap_case=True)
    log_dir(claude, root / "job-hay", 2000)
    log_dir(claude, root / "job-hay-old", 9000)
    log_dir(claude, root / "Anvita", 1000)
    log_dir(claude, root / "FooBar", 500)
    log_dir(claude, root / "foo-bar", 100)
    touch(claude / "rác.txt")

    extra = tmp_path / "elsewhere" / "my-app"
    touch(extra / "pyproject.toml")
    overrides = touch(tmp_path / "projects.toml", f"""
exclude = ["job-hay-old"]
[aliases]
"AI-assistent" = ["ai assistant", "trợ lý mimi"]
[extra]
"My App" = '{extra}'
""")
    return {"root": root, "claude": claude, "overrides": overrides, "extra": extra, "tmp": tmp_path}


@pytest.fixture
def reg(tree) -> ProjectRegistry:
    """Không overrides: có cả job-hay lẫn job-hay-old."""
    return ProjectRegistry([tree["root"], tree["tmp"] / "khong-co"], claude_dir=tree["claude"])


@pytest.fixture
def reg_ov(tree) -> ProjectRegistry:
    return ProjectRegistry([tree["root"]], overrides=tree["overrides"], claude_dir=tree["claude"])


# ---------- Quét ----------

def test_scan_layout(reg):
    names = {p.name for p in reg.all()}
    assert names == {"AI-assistent", "ScanOrderEnjoy", "job-hay", "job-hay-old", "fruit-rush",
                     "figma-console-mcp", "dotnet-app", "Shen/agentic-comics", "Shen/ai-management",
                     "learning-aws", "Anvita", "foo-bar", "FooBar"}


def test_all_sorted_by_last_used(reg):
    names = [p.name for p in reg.all()]
    assert names[:6] == ["job-hay-old", "AI-assistent", "job-hay", "Anvita", "FooBar", "foo-bar"]
    rest = names[6:]
    assert rest == sorted(rest, key=str.lower)
    assert all(reg.get(n).last_used is None for n in rest)


def test_last_used_and_case_insensitive_log_dir(reg, tree):
    p = reg.get("ai-assistent")
    assert p.last_used == pytest.approx(3000)
    dirs = claude_project_dirs(p.path, tree["claude"])
    assert len(dirs) == 1 and dirs[0].name.lower() == claude_slug(p.path).lower()
    assert dirs[0].name != claude_slug(p.path)        # khác hoa thường vẫn nhận
    # Đúng tên, không lấy thư mục của dự án có tên dài hơn (job-hay-old)
    assert [d.name for d in claude_project_dirs(tree["root"] / "job-hay", tree["claude"])] == \
        [claude_slug(tree["root"] / "job-hay")]
    assert claude_project_dirs(tree["root"] / "fruit-rush", tree["claude"]) == []
    assert claude_project_dirs(tree["root"], tree["tmp"] / "khong-co") == []


def test_slug():
    assert claude_slug(Path(r"D:\projects\AI-assistent\_local\claude-workdir")) == \
        "D--projects-AI-assistent--local-claude-workdir"


def test_description_and_stack(reg):
    ai = reg.get("AI-assistent")
    assert ai.description == "Trợ lý giọng nói local bọc quanh Claude."
    assert ai.stack == ["python"]
    soe = reg.get("ScanOrderEnjoy")
    assert soe.description.startswith("Ứng dụng gọi món") and len(soe.description) <= 160
    assert soe.stack == ["node"]
    assert reg.get("dotnet-app").stack == ["dotnet"]
    assert reg.get("learning-aws").description == ""
    d = reg.get("Shen/agentic-comics").to_dict()
    assert d["path"] == str(reg.get("Shen/agentic-comics").path)
    assert set(d) == {"name", "path", "aliases", "description", "stack", "last_used"}


def test_overrides(reg_ov, tree):
    names = {p.name for p in reg_ov.all()}
    assert "job-hay-old" not in names and "My App" in names
    assert all(p.name != "job-hay-old" for p, _ in reg_ov.match("job hay old"))
    my = reg_ov.get("my app")
    assert my.path == tree["extra"] and my.stack == ["python"]
    assert reg_ov.get(str(tree["extra"])) is my
    top, score = reg_ov.match("Trợ lý Mimi")[0]
    assert top.name == "AI-assistent" and score == 1.0
    assert reg_ov.get("AI-assistent").aliases == ["ai assistant", "trợ lý mimi"]


def test_refresh_sees_new_project(reg, tree):
    assert reg.get("new-thing") is None
    touch(tree["root"] / "new-thing" / "README.md", "x\n")
    reg.refresh()
    assert reg.get("new-thing") is not None


# ---------- So khớp tên ----------

def test_name_key():
    assert name_key("ScanOrderEnjoy") == "scanorderenjoi"
    assert name_key("AI-assistent") == name_key("ai assistent")
    assert name_key("job_hay") == name_key("Job Hay")


@pytest.mark.parametrize("spoken, expected", [
    ("job hay", "job-hay"),
    ("job hay old", "job-hay-old"),
    ("AI assistant", "AI-assistent"),
    ("ai assistant", "AI-assistent"),
    ("scan order enjoy", "ScanOrderEnjoy"),
    ("ScanOrderEnjoy", "ScanOrderEnjoy"),
    ("fruit rush", "fruit-rush"),
    ("phi gma console", "figma-console-mcp"),
    ("figma console", "figma-console-mcp"),
    ("agentic comics", "Shen/agentic-comics"),
    ("shen ai management", "Shen/ai-management"),
])
def test_match(reg, spoken, expected):
    matches = reg.match(spoken)
    assert matches and matches[0][0].name == expected
    assert all(0.6 <= s <= 1 for _, s in matches)
    assert [s for _, s in matches] == sorted((s for _, s in matches), reverse=True)


def test_exact_beats_prefix(reg):
    (p1, s1), (p2, s2) = reg.match("job hay")[:2]
    assert (p1.name, s1) == ("job-hay", 1.0)
    assert p2.name == "job-hay-old" and s2 < 0.9


def test_tie_prefers_recent(reg):
    assert [p.name for p, s in reg.match("foo bar")[:2]] == ["FooBar", "foo-bar"]


def test_no_match(reg):
    assert reg.match("xyz abc") == []
    assert reg.match("") == []


# ---------- Tách câu ----------

@pytest.mark.parametrize("text, project, query, req", [
    ("mở dự án job hay", "job-hay", "job hay", ""),
    ("mở dự án job hay nhé", "job-hay", "job hay", ""),
    ("Mimi ơi mở dự án job hay rồi thêm phân trang cho trang danh sách", "job-hay", "job hay",
     "thêm phân trang cho trang danh sách"),
    ("mở vs code dự án AI assistant, sửa lỗi nút dừng", "AI-assistent", "AI assistant", "sửa lỗi nút dừng"),
    ("trong dự án scan order enjoy viết test cho API đơn hàng", "ScanOrderEnjoy", "scan order enjoy",
     "viết test cho API đơn hàng"),
    ("open project fruit rush and fix the build", "fruit-rush", "fruit rush", "fix the build"),
    ("Mở dự án Job Hay rồi Thêm Nút Lọc Theo Lương.", "job-hay", "Job Hay", "Thêm Nút Lọc Theo Lương"),
    ("mở dự án job hay thêm phân trang", "job-hay", "job hay", "thêm phân trang"),
    ("mở dự án job hay old xem lại README", "job-hay-old", "job hay old", "xem lại README"),
    ("mở dự án figma console sửa lỗi build", "figma-console-mcp", "figma console", "sửa lỗi build"),
    ("mở dự án agentic comics", "Shen/agentic-comics", "agentic comics", ""),
    ("thêm phân trang cho dự án job hay", "job-hay", "job hay", "thêm phân trang"),
    ("mở job hay bằng vs code", "job-hay", "job hay", ""),
    ("mở dự án job hay trong vs code rồi chạy test", "job-hay", "job hay", "chạy test"),
])
def test_parse(reg, text, project, query, req):
    t = parse_dev_task(text, reg)
    assert t is not None
    assert t.project is not None and t.project.name == project
    assert t.candidates[0] is t.project and len(t.candidates) <= 3
    assert (t.project_query, t.request, t.raw) == (query, req, text)


def test_parse_unknown_project(reg):
    t = parse_dev_task("mở dự án xyz abc làm gì đó", reg)
    assert t is not None
    assert (t.project, t.candidates, t.project_query, t.request) == (None, [], "xyz abc", "làm gì đó")
    # Không có động từ mở/chuyển: câu hỏi thường, để Claude trả lời
    assert parse_dev_task("dự án microservice thường có những phần nào", reg) is None
    assert parse_dev_task("project reference trong dotnet dùng khi nào", reg) is None


def test_parse_ambiguous(reg):
    t = parse_dev_task("mở dự án job", reg)
    assert t is not None and t.project is None
    assert [p.name for p in t.candidates][:2] == ["job-hay", "job-hay-old"]


def test_parse_only_vscode(reg):
    t = parse_dev_task("mở vs code", reg)
    assert t is not None and (t.project, t.project_query, t.request) == (None, "", "")


@pytest.mark.parametrize("text", [
    "dự án là gì",
    "project management là gì",
    "làm sao mở project trong vs code",
    "cách tạo dự án .NET mới",
    "how do I open a project in vs code",
    "vs code có extension nào cho python",
    "dự án này có nên dùng Lambda không?",
    "EC2 là gì",
    "mở session mới",
    "",
])
def test_parse_rejects_questions(reg, text):
    assert parse_dev_task(text, reg) is None


def test_find_project_anywhere(tmp_path):
    from server.agent.intent import find_project_anywhere
    root = tmp_path / "projects"
    for name in ("job-hay", "job-hay-old", "fruit-rush", "dang"):
        (root / name).mkdir(parents=True)
        (root / name / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    reg = ProjectRegistry([root], None, tmp_path / "claude")
    reg.refresh()

    def parse(text):
        dev = find_project_anywhere(text, reg)
        return dev and (dev.project.name, dev.request)

    assert parse("Mở dạng job hay.") == ("job-hay", "")                       # STT nghe sai "dự án"
    assert parse("Mimi ơi fruit rush viết test cho game loop") == ("fruit-rush", "viết test cho game loop")
    assert parse("thêm phân trang cho job hay") == ("job-hay", "thêm phân trang")
    assert parse("sửa lỗi đăng nhập") is None                                  # "đăng" không phải dự án dang
    assert parse("viết test cho game loop") is None
