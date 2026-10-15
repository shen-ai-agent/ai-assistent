"""Test vòng tự học (server/learning.py, server/learn_api.py). Không cần GPU."""

from __future__ import annotations

import itertools
import json
import shutil

import pytest
from fastapi.testclient import TestClient

from server import learning as L
from server.app import create_app
from server.config import ROOT, Config
from server.stt.glossary import load_glossary
from server.stt.engine import Transcript


def ids(split: str, n: int) -> list[str]:
    """turn_id thuộc đúng split mong muốn."""
    gen = (f"20261016-1200{i:02d}-{i:04x}" for i in itertools.count())
    return list(itertools.islice((t for t in gen if L.split_of(t) == split), n))


def turn(tid: str, text: str, fixed: str | None, topic: str = "AWS") -> L.Turn:
    return L.Turn(tid, topic, text, text, fixed, None)


@pytest.fixture
def glossary():
    return load_glossary(ROOT / "data" / "glossary.txt")


# ---------------------------------------------------------------- dữ liệu


def test_split_is_stable_and_roughly_30_percent():
    all_ids = [f"20261016-120000-{i:04x}" for i in range(2000)]
    holdout = sum(L.split_of(t) == "holdout" for t in all_ids)
    assert 500 < holdout < 700
    assert all(L.split_of(t) == L.split_of(t) for t in all_ids[:50])


def test_load_turns_ignores_empty_and_unchanged_corrections(tmp_path):
    rows = [
        {"event": "turn", "turn_id": "a", "topic": "AWS", "raw": "x", "text": "x"},
        {"event": "turn", "turn_id": "b", "topic": "AWS", "raw": "y", "text": "y"},
        {"event": "turn", "turn_id": "c", "skipped": "no_speech"},
        {"event": "correction", "turn_id": "a", "text": "  "},
        {"event": "correction", "turn_id": "b", "text": "z"},
        {"event": "correction", "turn_id": "b", "text": "y"},  # sửa về như cũ
    ]
    (tmp_path / "turns-2026-10-16.jsonl").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    turns = {t.turn_id: t for t in L.load_turns(tmp_path)}
    assert set(turns) == {"a", "b"}
    assert turns["a"].fixed is None and turns["b"].fixed is None


def test_diff_spans_and_latin_phrases():
    assert L.diff_spans("cho tôi một Roboto Plan.", "cho tôi một Proposal Plan.") == [("Roboto", "Proposal")]
    assert L.latin_phrases("cho tôi một Proposal Plan nhé") == ["Proposal Plan"]
    assert L.latin_phrases("dùng JWTở đâu") == ["JWT"]  # gõ thiếu dấu cách
    assert L.latin_phrases("tuyến ăn cứt") == []


# ---------------------------------------------------------------- đề xuất


def test_suggest_needs_two_dev_turns(glossary):
    d = ids("dev", 2)
    one = [turn(d[0], "viết một Roboto Plan", "viết một Proposal Plan")]
    assert L.suggest(one, glossary, [], set()) == []

    two = one + [turn(d[1], "gửi Roboto cho sếp", "gửi Proposal cho sếp")]
    got = {(s.kind, s.wrong, s.right) for s in L.suggest(two, glossary, [], set())}
    assert ("rule", "Roboto", "Proposal") in got
    assert any(k == "term" and r.startswith("Proposal") for k, _, r in got)


def test_suggest_ignores_holdout_and_vietnamese(glossary):
    h = ids("holdout", 2)
    holdout = [turn(t, "viết một Roboto", "viết một Proposal") for t in h]
    assert L.suggest(holdout, glossary, [], set()) == []

    d = ids("dev", 2)
    vietnamese = [turn(t, "tuyến ăn cứt", "tuyển dụng gấp") for t in d]
    assert L.suggest(vietnamese, glossary, [], set()) == []


def test_suggest_skips_known_terms_rules_and_decided(glossary):
    d = ids("dev", 2)
    # "S3" đã có trong glossary: không đề xuất term; vế sai "EC2" là thuật ngữ đúng: không đề xuất rule
    turns = [turn(t, "dùng EC2 nhé", "dùng S3 nhé") for t in d]
    assert L.suggest(turns, glossary, [], set()) == []

    turns = [turn(t, "viết Roboto", "viết Proposal") for t in d]
    assert all(s.kind != "rule" for s in L.suggest(turns, glossary, [("roboto", "Proposal")], set()))
    decided = {s.id for s in L.suggest(turns, glossary, [], set())}
    assert L.suggest(turns, glossary, [], decided) == []


def test_suggest_unglues_fixed_text(glossary):
    d = ids("dev", 2)
    turns = [turn(t, "một GFDT ở đâu", "một JWTở đâu") for t in d]
    got = {(s.kind, s.wrong, s.right) for s in L.suggest(turns, glossary, [], set())}
    assert got == {("rule", "GFDT", "JWT")}  # JWT đã có trong glossary: không đề xuất term


def test_rule_warning_when_wrong_side_is_a_word():
    s = L.Suggestion("x", "rule", "integrate", wrong="interface")
    assert s.to_dict()["warning"]
    assert L.Suggestion("y", "rule", "Proposal", wrong="pờ rô pô").warning is None


# ---------------------------------------------------------------- kiểm tra / áp dụng


def test_check_rule_blocks_when_it_breaks_other_turns(monkeypatch, tmp_path):
    monkeypatch.setattr(L, "BASELINE_RUN", tmp_path / "none.csv")
    d = ids("dev", 3)
    s = L.Suggestion("x", "rule", "Proposal", wrong="Roboto")
    good = [turn(d[0], "viết Roboto", "viết Proposal"), turn(d[1], "gửi Roboto", "gửi Proposal")]
    r = L.check_rule(s, good, [])
    assert r["passed"] and r["turns"]["dev"]["better"] == 2

    bad = good + [turn(d[2], "robot Roboto đẹp", None)]  # người dùng nói "Roboto" thật
    r = L.check_rule(s, bad, [])
    assert not r["passed"] and r["turns"]["dev"]["worse"] == 1


def test_apply_term_goes_to_last_section_of_topic(tmp_path):
    path = tmp_path / "glossary.txt"
    shutil.copy(ROOT / "data" / "glossary.txt", path)
    L.apply(L.Suggestion("x", "term", "IELTS Speaking Band"), "Tiếng Anh", path, tmp_path / "c.tsv")
    L.apply(L.Suggestion("y", "term", "Step Functions"), "AWS", path, tmp_path / "c.tsv")
    terms = {t.text: t for t in load_glossary(path)}
    assert terms["IELTS Speaking Band"].topic == "Tiếng Anh"
    assert terms["Step Functions"].topic == "AWS" and not terms["Step Functions"].priority
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == "IELTS Speaking Band"


def test_apply_rule_appends_line(tmp_path):
    path = tmp_path / "c.tsv"
    path.write_text("# comment", encoding="utf-8")  # không có \n cuối
    L.apply(L.Suggestion("x", "rule", "Proposal", wrong="Roboto"), None, tmp_path / "g.txt", path)
    assert path.read_text(encoding="utf-8") == "# comment\nRoboto\tProposal\n"


# ---------------------------------------------------------------- API


class FakeEngine:
    def transcribe(self, audio, **_):
        return Transcript("viết Roboto", "vi", 1.0)


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "BASELINE_RUN", tmp_path / "none.csv")
    monkeypatch.setattr(L, "TESTSET", tmp_path / "testset")
    shutil.copy(ROOT / "data" / "glossary.txt", tmp_path / "glossary.txt")
    cfg = Config(log_dir=tmp_path / "logs", min_speech_ms=0,
                 glossary=tmp_path / "glossary.txt", corrections=tmp_path / "corrections.tsv")
    cfg.log_dir.mkdir()
    rows = []
    for t in ids("dev", 2):
        rows += [{"event": "turn", "turn_id": t, "topic": "Dev chung", "raw": "viết Roboto", "text": "viết Roboto"},
                 {"event": "correction", "turn_id": t, "text": "viết Proposal"}]
    (cfg.log_dir / "turns-2026-10-16.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    with TestClient(create_app(cfg, engine_factory=lambda _: FakeEngine())) as client:
        yield client, cfg


def test_api_flow_rule(api):
    client, cfg = api
    data = client.get("/api/learn").json()
    assert data["stats"]["fixed"] == 2
    rule = next(s for s in data["suggestions"] if s["kind"] == "rule")

    assert client.post(f"/api/learn/{rule['id']}/approve", json={}).status_code == 409  # chưa kiểm tra
    assert client.post(f"/api/learn/{rule['id']}/check", json={}).json()["passed"]
    assert client.post(f"/api/learn/{rule['id']}/approve", json={}).status_code == 200

    assert cfg.corrections.read_text(encoding="utf-8") == "Roboto\tProposal\n"
    left = client.get("/api/learn").json()["suggestions"]
    assert rule["id"] not in {s["id"] for s in left}
    assert (cfg.log_dir.parent / "learning" / "decisions.jsonl").exists()


def test_api_term_needs_topic_and_reject(api):
    client, cfg = api
    term = next(s for s in client.get("/api/learn").json()["suggestions"] if s["kind"] == "term")
    assert term["topic"] == "Dev chung"
    assert client.post(f"/api/learn/{term['id']}/check", json={"topic": "Không có"}).status_code == 400

    r = client.post(f"/api/learn/{term['id']}/check", json={"topic": "Dev chung"}).json()
    assert r["passed"] and r["topic"] == "Dev chung"
    # Kiểm tra với chủ đề này không dùng được để duyệt chủ đề khác
    assert client.post(f"/api/learn/{term['id']}/approve", json={"topic": "AWS"}).status_code == 409
    assert client.post(f"/api/learn/{term['id']}/approve", json={"topic": "Dev chung"}).status_code == 200
    assert any(t.text == "Proposal" and t.topic == "Dev chung" for t in load_glossary(cfg.glossary))

    rule = next(s for s in client.get("/api/learn").json()["suggestions"] if s["kind"] == "rule")
    assert client.post(f"/api/learn/{rule['id']}/reject").status_code == 200
    assert client.get("/api/learn").json()["suggestions"] == []
