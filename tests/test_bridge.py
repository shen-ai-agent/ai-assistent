"""Test bridge không cần Claude: feeder cắt câu, parser stream-json, ClaudeCliBridge với tiến trình giả."""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
import time
from contextlib import aclosing

import pytest

from server.bridge import BridgeError, Done, SentenceFeeder, TextDelta
from server.bridge.claude_cli import ClaudeCliBridge, StreamParser, load_prompt
from server.bridge.clipboard import parse_hotkey


def feed_all(deltas: list[str]) -> list[str]:
    f = SentenceFeeder()
    out = [c for d in deltas for c in f.push(d)]
    return out + f.flush()


def test_feeder_cuts_at_sentence_end():
    assert feed_all(["Chào bạn. EC2 là", " máy ảo! Còn S3", " thì sao?"]) == ["Chào bạn.", " EC2 là máy ảo!", " Còn S3 thì sao?"]


def test_feeder_emits_first_sentence_early():
    f = SentenceFeeder()
    assert f.push("Chào bạn.") == []  # chưa biết có phải "3.5" không
    assert f.push(" Hôm") == ["Chào bạn."]


def test_feeder_keeps_decimals_and_files():
    assert feed_all(["Python 3.11 đọc file config.toml rồi chạy."]) == ["Python 3.11 đọc file config.toml rồi chạy."]


def test_feeder_no_cut_inside_code_fence():
    out = feed_all(["Ví dụ:\n```py\nx = 1. y = 2\n", "print(x)\n```\nXong."])
    assert out == ["Ví dụ:\n", "```py\nx = 1. y = 2\nprint(x)\n```\n", "Xong."]


def test_feeder_stops_at_code_marker_split_across_deltas():
    out = feed_all(["Dùng lệnh dưới đây.\n---CO", "DE---\naws s3 ls. Thêm nữa."])
    assert out == ["Dùng lệnh dưới đây.\n"]


def test_feeder_closes_unfinished_fence():
    assert feed_all(["```py\nx = 1"]) == ["```py\nx = 1\n```"]


def test_load_prompt_takes_part_after_separator():
    prompt = load_prompt(__import__("server.config", fromlist=["ROOT"]).ROOT / "prompts" / "voice_reply.md")
    assert "---CODE---" in prompt and not prompt.startswith("---")


def ev(text):
    return {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}}


def test_stream_parser():
    p = StreamParser()
    assert p.feed({"type": "system", "subtype": "init", "session_id": "abc"}) == []
    assert p.feed(ev("Xin")) == [TextDelta("Xin")]
    # Đoạn text thứ hai (sau khi dùng WebSearch) tách bằng xuống dòng để feeder cắt câu
    assert p.feed({"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "text"}}}) == [TextDelta("\n")]
    p.feed({"type": "result", "subtype": "success", "is_error": False, "session_id": "abc", "total_cost_usd": 0.02})
    assert p.done() == Done("abc", 0.02)


def test_stream_parser_errors():
    p = StreamParser()
    with pytest.raises(BridgeError, match="không trả kết quả"):
        p.done()
    p.feed({"type": "result", "subtype": "success", "is_error": True, "result": "Credit balance is too low"})
    with pytest.raises(BridgeError, match="Credit"):
        p.done()


FAKE_CLAUDE = textwrap.dedent('''
    import json, sys, time
    args = sys.argv[1:]
    sid = args[args.index("--resume") + 1] if "--resume" in args else "new-session"
    if "--fork-session" in args:
        sid += "-fork"
    def out(o):
        print(json.dumps(o), flush=True)
    model = args[args.index("--model") + 1] if "--model" in args else "default"
    out({"type": "system", "subtype": "init", "session_id": sid, "model": "claude-" + model})
    prompt = args[args.index("-p") + 1]
    if prompt == "crash":
        print("Error: not logged in", file=sys.stderr)
        sys.exit(1)
    for word in ["Câu một. ", "Câu hai."]:
        out({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": word}}})
        if prompt == "slow":
            time.sleep(30)
    out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "total_cost_usd": 0.03, "result": ""})
''')


@pytest.fixture
def fake_bridge(tmp_path):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("ghi chú\n---\nNói ngắn.", encoding="utf-8")
    bridge = ClaudeCliBridge(tmp_path / "work", prompt, ["WebSearch"], timeout_s=10, exe=sys.executable)
    real = bridge.command
    bridge.command = lambda text: [sys.executable, str(script), *real(text)[1:]]
    return bridge


async def collect(bridge, text):
    return [e async for e in bridge.ask(text)]


def test_cli_bridge_streams_and_resumes(fake_bridge):
    events = asyncio.run(collect(fake_bridge, "S3 là gì?"))
    assert events == [TextDelta("Câu một. "), TextDelta("Câu hai."), Done("new-session", 0.03, "claude-default")]
    assert fake_bridge.session_id == "new-session"
    cmd = fake_bridge.command("tiếp")
    assert cmd[cmd.index("--resume") + 1] == "new-session"
    assert "--fork-session" in cmd
    assert cmd[cmd.index("--append-system-prompt") + 1] == "Nói ngắn."
    assert json.loads(cmd[cmd.index("--settings") + 1])["claudeMdExcludes"] == ["**/CLAUDE.md"]
    fake_bridge.model = "haiku"
    assert asyncio.run(collect(fake_bridge, "tiếp"))[-1].model == "claude-haiku"
    assert fake_bridge.session_id == "new-session-fork"
    fake_bridge.new_session()
    assert "--resume" not in fake_bridge.command("mới")


def test_cli_bridge_cancelled_turn_keeps_session(fake_bridge):
    """Lượt bị ngắt không thành ngữ cảnh: lượt sau resume từ session trước đó, không phải bản fork dở dang."""
    asyncio.run(collect(fake_bridge, "S3 là gì?"))

    async def cancel():
        async with aclosing(fake_bridge.ask("slow")) as events:
            async for _ in events:
                break

    asyncio.run(cancel())
    assert fake_bridge.session_id == "new-session"
    with pytest.raises(BridgeError):
        asyncio.run(collect(fake_bridge, "crash"))
    assert fake_bridge.session_id == "new-session"


def test_cli_bridge_process_error(fake_bridge):
    with pytest.raises(BridgeError, match="not logged in"):
        asyncio.run(collect(fake_bridge, "crash"))


def test_cli_bridge_cancel_kills_process(fake_bridge):
    async def run():
        t0 = time.perf_counter()
        async with aclosing(fake_bridge.ask("slow")) as events:
            async for e in events:
                assert e == TextDelta("Câu một. ")
                break  # đóng generator giữa chừng, tiến trình đang sleep 30s
        return time.perf_counter() - t0

    assert asyncio.run(run()) < 10


def test_cli_bridge_timeout(fake_bridge):
    fake_bridge.timeout_s = 1
    with pytest.raises(BridgeError, match="không trả lời xong"):
        asyncio.run(collect(fake_bridge, "slow"))


def test_parse_hotkey():
    assert parse_hotkey("ctrl+alt+r") == (0x2 | 0x1 | 0x4000, ord("R"))
    assert parse_hotkey("Ctrl+Shift+F9")[1] == 0x78
    with pytest.raises(ValueError):
        parse_hotkey("ctrl+hyper+r")
    with pytest.raises(ValueError):
        parse_hotkey("ctrl+space")
