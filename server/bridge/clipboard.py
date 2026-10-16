"""Bridge clipboard (4a): dùng với Claude Desktop/web, không cần API hay CLI.

Transcript được chép vào clipboard để dán vào Claude. Copy câu trả lời của Claude rồi bấm hotkey
(mặc định Ctrl+Alt+R) thì server đọc clipboard và đọc thành tiếng. Hotkey là hotkey toàn cục của Windows,
bấm được khi đang ở cửa sổ Claude.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
import threading
from ctypes import wintypes
from typing import AsyncIterator, Callable

from .base import BridgeError, Done, TextDelta

log = logging.getLogger("bridge")

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000

if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32.OpenClipboard.argtypes = [wintypes.HWND]
    _user32.GetClipboardData.restype = wintypes.HANDLE
    _user32.GetClipboardData.argtypes = [wintypes.UINT]
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _kernel32.GlobalLock.restype = ctypes.c_void_p
    _kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    _user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    _user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    _user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]


def _open_clipboard():
    # Ứng dụng khác có thể đang giữ clipboard: thử lại vài lần
    for _ in range(20):
        if _user32.OpenClipboard(None):
            return
        threading.Event().wait(0.02)
    raise BridgeError("Không mở được clipboard (ứng dụng khác đang giữ)")


def get_clipboard() -> str:
    _open_clipboard()
    try:
        h = _user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = _kernel32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p)
        finally:
            _kernel32.GlobalUnlock(h)
    finally:
        _user32.CloseClipboard()


def set_clipboard(text: str) -> None:
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    _open_clipboard()
    try:
        _user32.EmptyClipboard()
        h = _kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        p = _kernel32.GlobalLock(h)
        ctypes.memmove(p, data, size)
        _kernel32.GlobalUnlock(h)
        if not _user32.SetClipboardData(CF_UNICODETEXT, h):
            _kernel32.GlobalFree(h)  # thành công thì clipboard sở hữu h, thất bại thì phải tự giải phóng
            raise BridgeError("Không ghi được clipboard")
    finally:
        _user32.CloseClipboard()


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+alt+r' → (modifiers, virtual key). Chỉ nhận chữ cái, số, F1–F12."""
    *mods, key = [p.strip().lower() for p in spec.split("+")]
    try:
        mod = sum(MOD[m] for m in mods)
    except KeyError as e:
        raise ValueError(f"Hotkey {spec!r}: không biết phím {e.args[0]!r}") from None
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 12:
        vk = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError(f"Hotkey {spec!r}: phím {key!r} không hỗ trợ")
    return mod | MOD_NOREPEAT, vk


class HotkeyListener:
    """RegisterHotKey cần một thread có message loop; mỗi lần bấm gọi callback trên event loop."""

    def __init__(self, spec: str, callback: Callable[[], None], loop: asyncio.AbstractEventLoop):
        self.mod, self.vk = parse_hotkey(spec)
        self.spec, self.callback, self.loop = spec, callback, loop
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self.error: str | None = None
        self._thread = threading.Thread(target=self._run, name="hotkey", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(2)
        if self.error:
            raise BridgeError(self.error)

    def stop(self) -> None:
        if self._thread_id:
            _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            self._thread.join(2)

    def _run(self):
        self._thread_id = _kernel32.GetCurrentThreadId()
        if not _user32.RegisterHotKey(None, 1, self.mod, self.vk):
            self.error = f"Không đăng ký được hotkey {self.spec} (ứng dụng khác đang dùng?)"
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    self.loop.call_soon_threadsafe(self.callback)
        finally:
            _user32.UnregisterHotKey(None, 1)


class ClipboardBridge:
    """ask(): chép câu hỏi vào clipboard rồi chờ hotkey. Hotkey khi không có lượt nào chờ thì gọi on_orphan
    với nội dung clipboard (app đọc nó ở kết nối gần nhất), để vẫn dùng được khi gõ câu hỏi trong Claude."""

    name = "clipboard"
    session_id = None

    def __init__(self, hotkey: str = "ctrl+alt+r", timeout_s: float = 600):
        self.hotkey = hotkey
        self.timeout_s = timeout_s
        self.on_orphan: Callable[[str], None] | None = None
        self._waiter: asyncio.Future | None = None
        self._listener: HotkeyListener | None = None

    def start(self) -> None:
        self._listener = HotkeyListener(self.hotkey, self._on_hotkey, asyncio.get_running_loop())
        self._listener.start()

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()

    def new_session(self) -> None:
        pass  # session nằm bên Claude Desktop/web

    def _on_hotkey(self):
        try:
            text = get_clipboard()
        except BridgeError as e:
            log.warning("%s", e)
            return
        if self._waiter and not self._waiter.done():
            self._waiter.set_result(text)
        elif self.on_orphan and text.strip():
            self.on_orphan(text)

    async def ask(self, text: str) -> AsyncIterator[TextDelta | Done]:
        await asyncio.to_thread(set_clipboard, text)
        if self._waiter and not self._waiter.done():
            self._waiter.cancel()  # lượt cũ chưa nhận câu trả lời: lượt mới thay chỗ
        self._waiter = asyncio.get_running_loop().create_future()
        try:
            async with asyncio.timeout(self.timeout_s):
                answer = await self._waiter
        except TimeoutError:
            raise BridgeError(f"Chưa nhận câu trả lời sau {self.timeout_s:.0f} giây") from None
        if answer.strip() == text.strip():
            raise BridgeError("Clipboard vẫn là câu hỏi: copy câu trả lời của Claude trước khi bấm hotkey")
        yield TextDelta(answer)
        yield Done()
