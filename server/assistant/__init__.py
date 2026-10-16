"""Trợ lý rảnh tay (T-010): VAD theo luồng, cắt đoạn nói, nhận tên gọi ("Mimi ơi")."""

from .vad import Segment, Segmenter, StreamingVad
from .wake import WakeMatch, WakeMatcher

__all__ = ["Segment", "Segmenter", "StreamingVad", "WakeMatch", "WakeMatcher"]
