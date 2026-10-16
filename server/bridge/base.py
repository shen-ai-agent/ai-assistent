"""Interface chung của bridge (Phase 4). STT/TTS chỉ biết interface này; chọn bridge qua config.

ask() là async generator: yield TextDelta khi có text, cuối cùng yield Done. Lỗi thì raise BridgeError.
Huỷ = huỷ task đang lặp ask(): generator bị đóng, bridge tự dọn (kill tiến trình, bỏ chờ hotkey).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol


@dataclass
class TextDelta:
    text: str


@dataclass
class Done:
    session_id: str | None = None
    cost_usd: float | None = None
    model: str | None = None  # tên model thật đã trả lời (claude_cli)


class BridgeError(Exception):
    """Lỗi đọc được cho người dùng nghe (chưa đăng nhập, hết hạn mức, timeout...)."""


class Bridge(Protocol):
    name: str
    session_id: str | None

    def ask(self, text: str) -> AsyncIterator[TextDelta | Done]: ...

    def new_session(self) -> None: ...

# Bridge chọn được model (claude_cli) có thêm thuộc tính model: str, rỗng = mặc định
