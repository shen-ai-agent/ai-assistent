"""Mở VS Code ở thư mục dự án và đưa yêu cầu vào ô chat Claude Code (T-011).

Extension anthropic.claude-code có URI handler `vscode://anthropic.claude-code/open?prompt=...&session=...`:
nó mở tab Claude mới và chỉ điền sẵn prompt vào ô nhập, không tự gửi. VS Code chuyển URI tới cửa sổ được
focus gần nhất, nên phải đưa đúng cửa sổ dự án lên trước rồi mới mở URI, sau đó nhấn Enter để gửi.

Mọi lời gọi hệ thống nằm trong `WinBackend` để test thay bằng backend giả.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode

log = logging.getLogger("agent")

CLAUDE_URI = "vscode://anthropic.claude-code/open"
VSCODE_MARK = "visual studio code"
POLL_S = 0.3

SW_RESTORE = 9
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_RETURN = 0x0D
VK_MENU = 0x12
VK_DUMMY = 0xE8  # phím ảo chưa gán: chèn giữa Alt xuống/lên để Alt không bật menu của ứng dụng nào
CREATE_NO_WINDOW = 0x08000000


def claude_uri(prompt: str | None = None, session: str | None = None) -> str:
    """URI mở tab Claude Code. Mã hoá bằng %XX (dấu cách thành %20, không dùng '+')."""
    params = {k: v for k, v in (("prompt", prompt), ("session", session)) if v}
    if not params:
        return CLAUDE_URI
    return CLAUDE_URI + "?" + urlencode(params, quote_via=quote, safe="")


def title_matches(title: str, folder: str) -> bool:
    """Tiêu đề kiểu "● app.py - job-hay - Visual Studio Code" có một đoạn (tách bởi -, –, —) đúng bằng tên
    thư mục, không phân biệt hoa thường. So theo đoạn để "job" không khớp "jobhay". Chấp nhận hậu tố
    " (Workspace)", " [SSH: ...]"."""
    t = title.casefold()
    name = folder.casefold().strip()
    if VSCODE_MARK not in t or not name:
        return False
    for part in re.split(r"\s[-\u2013\u2014]\s", t):
        part = part.strip(" \u25cf\u2022*")
        if part == name or part.startswith(name + " (") or part.startswith(name + " ["):
            return True
    # Tên thư mục tự chứa " - ": tách đoạn không dùng được, so nguyên cụm có dấu phân cách hai bên
    return bool(re.search(r"(^|\s[-–—]\s)[\s●•*]*" + re.escape(name) + r"\s[-–—]\s", t))


@dataclass
class LaunchResult:
    opened: bool        # có cửa sổ VS Code của dự án
    prefilled: bool     # đã mở URI (prompt nằm trong ô chat)
    submitted: bool     # đã nhấn Enter vào đúng cửa sổ
    note: str = ""      # tiếng Việt, đọc được cho người dùng khi có gì trục trặc


# ---------------------------------------------------------------------------------------------------------
# Win32

if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        # Đủ cả ba loại để sizeof(INPUT) đúng như Windows mong đợi (SendInput kiểm cbSize)
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    _user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.BringWindowToTop.argtypes = [wintypes.HWND]
    _user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    _user32.SendInput.restype = wintypes.UINT
    _user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]


def _keys(*events: tuple[int, bool]) -> int:
    """Gửi chuỗi phím (vk, nhả?) bằng SendInput. Trả số sự kiện đã gửi."""
    arr = (INPUT * len(events))()
    for i, (vk, up) in enumerate(events):
        arr[i].type = INPUT_KEYBOARD
        arr[i].u.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0)
    return _user32.SendInput(len(events), ctypes.byref(arr), ctypes.sizeof(INPUT))


def _hwnd(h) -> int:
    return int(h or 0)


def _code_direct(exe: str) -> tuple[str, str] | None:
    """Đọc code.cmd lấy (Code.exe, cli.js) để chạy thẳng, khỏi qua cmd.exe (đường dẫn có ký tự đặc biệt của
    batch như & % ^ sẽ hỏng). code.cmd dạng: "%~dp0..\\Code.exe" "%~dp0..\\<hash>\\resources\\app\\out\\cli.js" %*"""
    p = Path(exe)
    if p.suffix.lower() != ".cmd":
        return None
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'"%~dp0([^"]*Code[^"\\]*\.exe)"\s+"%~dp0([^"]*cli\.js)"', text, re.IGNORECASE)
    if not m:
        return None
    base = str(p.parent) + "\\"
    exe_path, cli = Path(base + m.group(1)), Path(base + m.group(2))
    if exe_path.is_file() and cli.is_file():
        return str(exe_path.resolve()), str(cli.resolve())
    return None


class WinBackend:
    """Lớp mỏng gom mọi lời gọi hệ thống. Các hàm đều đồng bộ (chặn), launcher gọi qua asyncio.to_thread."""

    def run_code(self, exe: str, path: Path) -> None:
        """Chạy `code <path>`, không chờ. Ưu tiên gọi thẳng Code.exe + cli.js như code.cmd làm."""
        direct = _code_direct(exe)
        flags = CREATE_NO_WINDOW if sys.platform == "win32" else 0
        if direct:
            env = dict(os.environ, ELECTRON_RUN_AS_NODE="1")
            env.pop("VSCODE_DEV", None)
            args = [direct[0], direct[1], str(path)]
        else:
            if exe.lower().endswith((".cmd", ".bat")) and re.search(r'[&|<>^%"!]', str(path)):
                raise OSError(f"đường dẫn có ký tự đặc biệt, không chạy qua {Path(exe).name} được")
            env, args = None, [exe, str(path)]
        subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)

    def list_windows(self) -> list[tuple[int, str]]:
        """Các cửa sổ top-level đang hiển thị và có tiêu đề: [(hwnd, title)]."""
        out: list[tuple[int, str]] = []

        def cb(hwnd, _lparam):
            if _user32.IsWindowVisible(hwnd):
                n = _user32.GetWindowTextLengthW(hwnd)
                if n > 0:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    _user32.GetWindowTextW(hwnd, buf, n + 1)
                    out.append((_hwnd(hwnd), buf.value))
            return True

        _user32.EnumWindows(WNDENUMPROC(cb), 0)
        return out

    def foreground(self) -> int:
        return _hwnd(_user32.GetForegroundWindow())

    def focus(self, hwnd: int) -> bool:
        """Đưa cửa sổ lên foreground. SetForegroundWindow thường bị Windows chặn khi tiến trình nền gọi, nên
        thử lần lượt: gọi thẳng → AttachThreadInput vào thread đang giữ foreground → mẹo phím Alt giả
        (Alt xuống, phím ảo chưa gán, Alt lên: không bật menu). Sau mỗi cách kiểm lại foreground."""
        if _user32.IsIconic(hwnd):
            _user32.ShowWindow(hwnd, SW_RESTORE)

        def ok() -> bool:
            time.sleep(0.05)
            return self.foreground() == hwnd

        if self.foreground() == hwnd:
            return True
        _user32.SetForegroundWindow(hwnd)
        if ok():
            return True

        fg = self.foreground()
        me = _kernel32.GetCurrentThreadId()
        other = _user32.GetWindowThreadProcessId(fg, None) if fg else 0
        if other and other != me and _user32.AttachThreadInput(me, other, True):
            try:
                _user32.BringWindowToTop(hwnd)
                _user32.SetForegroundWindow(hwnd)
            finally:
                _user32.AttachThreadInput(me, other, False)
            if ok():
                return True

        _keys((VK_MENU, False), (VK_DUMMY, False), (VK_DUMMY, True), (VK_MENU, True))
        _user32.SetForegroundWindow(hwnd)
        _user32.BringWindowToTop(hwnd)
        return ok()

    def open_uri(self, uri: str) -> None:
        os.startfile(uri)  # type: ignore[attr-defined]  # chỉ có trên Windows

    def send_enter(self) -> None:
        if _keys((VK_RETURN, False), (VK_RETURN, True)) != 2:
            raise OSError(f"SendInput thất bại (lỗi {ctypes.get_last_error()})")


# ---------------------------------------------------------------------------------------------------------


class VsCodeLauncher:
    """open(): mở thư mục trong VS Code, điền yêu cầu vào ô chat Claude Code và (tuỳ chọn) nhấn Enter.

    Không bao giờ raise vì lỗi hệ thống: trả LaunchResult có note. Mọi chờ đợi là async."""

    def __init__(self, exe: str | None = None, auto_submit: bool = True, window_wait_s: float = 15.0,
                 ready_wait_s: float = 3.0, submit_delay_s: float = 1.5, backend: "WinBackend | None" = None):
        self.exe = exe
        self.auto_submit = auto_submit
        self.window_wait_s = window_wait_s
        self.ready_wait_s = ready_wait_s
        self.submit_delay_s = submit_delay_s
        self.backend = backend if backend is not None else WinBackend()

    async def open(self, path: Path, prompt: str | None = None, session: str | None = None) -> LaunchResult:
        try:
            return await self._open(Path(path), prompt or None, session or None)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # lỗi Win32/tiến trình: báo cho người dùng, không làm sập lượt
            log.exception("Mở VS Code thất bại")
            return LaunchResult(False, False, False, f"Lỗi khi điều khiển VS Code: {e}")

    async def _open(self, path: Path, prompt: str | None, session: str | None) -> LaunchResult:
        b = self.backend
        folder = path.name or str(path)
        exe = self.exe or shutil.which("code")
        if not exe:
            return LaunchResult(False, False, False, "Không tìm thấy lệnh code của VS Code trong PATH.")

        before = {h for h, _ in await asyncio.to_thread(b.list_windows)}
        try:
            await asyncio.to_thread(b.run_code, exe, path)
        except OSError as e:
            return LaunchResult(False, False, False, f"Không chạy được VS Code: {e}")

        found = await self._find_window(folder, before)
        if found is None:
            return LaunchResult(False, False, False,
                                f"Không thấy cửa sổ VS Code của {folder} sau {self.window_wait_s:.0f} giây.")
        hwnd, is_new = found
        if is_new:
            await asyncio.sleep(self.ready_wait_s)  # cửa sổ mới: chờ extension Claude kích hoạt

        focused = await asyncio.to_thread(b.focus, hwnd)
        if prompt is None and session is None:
            return LaunchResult(True, False, False)
        if not focused:
            # URI sẽ rơi vào cửa sổ VS Code focus gần nhất, có thể là dự án khác: không mở
            return LaunchResult(True, False, False,
                                f"Đã mở VS Code ở {folder} nhưng không đưa được cửa sổ lên trước, "
                                "nên chưa điền yêu cầu. Bấm vào cửa sổ đó rồi thử lại.")

        await asyncio.to_thread(b.open_uri, claude_uri(prompt, session))
        if prompt is None:
            return LaunchResult(True, True, False)
        if not self.auto_submit:
            return LaunchResult(True, True, False, "Đã điền sẵn yêu cầu, bấm Enter trong VS Code để gửi.")

        await asyncio.sleep(self.submit_delay_s)
        if await asyncio.to_thread(b.foreground) != hwnd:
            return LaunchResult(True, True, False,
                                "Cửa sổ VS Code không còn ở trên cùng nên chưa gửi. "
                                "Đã điền sẵn yêu cầu, bấm Enter trong VS Code để gửi.")
        await asyncio.to_thread(b.send_enter)
        return LaunchResult(True, True, True)

    async def _find_window(self, folder: str, before: set[int]) -> tuple[int, bool] | None:
        """Chờ cửa sổ có tiêu đề khớp tên thư mục. Ưu tiên cửa sổ mới (không có trong `before`); nếu chỉ có
        cửa sổ cũ thì dùng nó (code focus cửa sổ đã mở thư mục đó), ưu tiên cái đang ở foreground."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.window_wait_s
        while True:
            wins = await asyncio.to_thread(self.backend.list_windows)
            matches = [h for h, t in wins if title_matches(t, folder)]
            new = [h for h in matches if h not in before]
            if new:
                return new[0], True
            if matches:
                fg = await asyncio.to_thread(self.backend.foreground)
                return (fg if fg in matches else matches[0]), False
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(POLL_S)
