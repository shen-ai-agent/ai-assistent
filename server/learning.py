"""Vòng tự học có kiểm soát (T-007): học từ các lượt người dùng sửa tay trên web.

  log lượt nói + bản sửa ──► đề xuất ──► kiểm tra tự động ──► người dùng duyệt ──► ghi vào data/
                            (≥2 lượt)    (bộ test + log)      (web /learn.html)

Hai loại đề xuất:
  term  thuật ngữ tiếng Anh người dùng hay phải sửa mà glossary chưa có → thêm vào glossary của chủ đề.
        Đòn bẩy lớn nhất: ở Phase 1 thuật ngữ có trong prompt đúng 92%, không có ~59%.
  rule  cặp (STT viết sai → đúng) lặp lại → thêm vào data/corrections.tsv.

Quy tắc giống benchmark: lượt nói chia dev/holdout cố định theo turn_id. Chỉ lượt dev sinh đề xuất;
lượt holdout chỉ dùng để kiểm chứng. Đề xuất làm điểm giảm (bộ test hoặc lượt của người dùng) thì bị chặn.
"""

from __future__ import annotations

import csv
import difflib
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import jiwer

from .config import ROOT
from .stt.corrections import Corrector
from .stt.glossary import Term, add_term, build_prompt, detect_topic
from .stt.textnorm import contains_term, normalize

TESTSET = ROOT / "data" / "testset"
# Kết quả benchmark của cấu hình đang dùng; rule mới được chấm lại trên transcript thô của nó
BASELINE_RUN = ROOT / "_local" / "bench" / "stt" / "v3_vi_topic_oracle" / "per_utt.csv"
HOLDOUT_PCT = 30
MIN_TURNS = 2           # đề xuất khi lỗi lặp ở ít nhất 2 lượt dev khác nhau
PROMPT_TOKEN_LIMIT = 223  # Whisper chỉ giữ chừng này token cuối của prompt
_PUNCT = ".,!?;:\"'()[]…"
# Âm tiết tiếng Việt viết không dấu hay gặp; viết thường mới tính (chữ hoa có thể là tên riêng)
_VI_ASCII = set("""
a anh ba bao cho chi co con cung da em gi giao hay hon khi kia la lam mai minh mot nao nay nha nhanh
nhu nhung noi qua ra roi sao sau tai tam thi thoi tim toi tu va vao vay vi voi xin xong
""".split())


# ---------------------------------------------------------------- dữ liệu


def split_of(turn_id: str) -> str:
    """Chia dev/holdout cố định theo turn_id (không đổi khi log dài thêm)."""
    return "holdout" if int(hashlib.sha1(turn_id.encode()).hexdigest(), 16) % 100 < HOLDOUT_PCT else "dev"


@dataclass
class Turn:
    turn_id: str
    topic: str | None
    raw: str            # Whisper trả về
    text: str           # sau corrections, là cái người dùng thấy
    fixed: str | None   # bản người dùng sửa (None = không sửa)
    audio: Path | None

    @property
    def split(self) -> str:
        return split_of(self.turn_id)

    @property
    def final(self) -> str:
        """Text coi là đúng: bản sửa nếu có, không thì text đã hiện (người dùng không thấy sai)."""
        return self.fixed if self.fixed is not None else self.text


def load_turns(log_dir: Path) -> list[Turn]:
    turns: dict[str, Turn] = {}
    for path in sorted(log_dir.glob("turns-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r["event"] == "turn" and "raw" in r:  # lượt bị bỏ (no_speech) không có raw
                audio = log_dir / r["audio_file"] if r.get("audio_file") else None
                turns[r["turn_id"]] = Turn(r["turn_id"], r.get("topic"), r["raw"], r["text"], None, audio)
            elif r["event"] == "correction" and r["turn_id"] in turns:
                fixed = r["text"].strip()
                t = turns[r["turn_id"]]
                t.fixed = fixed if fixed and fixed != t.text else None  # xoá trắng / sửa về như cũ = không sửa
    return list(turns.values())


# ---------------------------------------------------------------- đề xuất


def diff_spans(before: str, after: str) -> list[tuple[str, str]]:
    """Các đoạn bị thay (theo từ) giữa hai câu, bỏ dấu câu hai đầu."""
    a, b = before.split(), after.split()
    sm = difflib.SequenceMatcher(a=[w.lower().strip(_PUNCT) for w in a],
                                 b=[w.lower().strip(_PUNCT) for w in b], autojunk=False)
    return [(" ".join(a[i1:i2]).strip(_PUNCT), " ".join(b[j1:j2]).strip(_PUNCT))
            for op, i1, i2, j1, j2 in sm.get_opcodes() if op == "replace"]


def _is_latin(word: str) -> bool:
    """Từ tiếng Anh / thuật ngữ: chỉ ký tự ASCII, có chữ cái. Tiếng Việt có dấu thì không."""
    w = word.strip(_PUNCT)
    return bool(w) and w.isascii() and any(c.isalpha() for c in w)


def _unglue(token: str) -> list[str]:
    """Tách chữ viết tắt dính với từ tiếng Việt do gõ thiếu dấu cách: "JWTở" → ["JWT", "ở"].

    Chỉ tách khi phần đầu là ≥2 chữ in hoa/số ASCII và chữ ngay sau là chữ thường có dấu,
    để không cắt vụn từ tiếng Việt bình thường ("tôi", "THƯ").
    """
    m = re.match(r"[A-Z0-9]{2,}", token)
    if m and m.end() < len(token) and not token[m.end()].isascii() and token[m.end()].islower():
        return [token[: m.end()], token[m.end():]]
    return [token]


def latin_phrases(text: str) -> list[str]:
    """Các cụm từ ASCII liền nhau: "cho tôi một Proposal Plan" → ["Proposal Plan"]."""
    out, cur = [], []
    for w in [w for tok in text.split() for w in _unglue(tok)] + [""]:
        if w and _is_latin(w):
            cur.append(w.strip(_PUNCT))
        elif cur:
            # Âm tiết tiếng Việt không dấu ở hai đầu ("cho Proposal") không thuộc thuật ngữ
            while cur and cur[0] in _VI_ASCII:
                cur.pop(0)
            while cur and cur[-1] in _VI_ASCII:
                cur.pop()
            if cur:
                out.append(" ".join(cur))
            cur = []
    return out


def in_glossary(phrase: str, glossary: list[Term]) -> bool:
    key = normalize(phrase).replace(" ", "")
    return any(normalize(s).replace(" ", "") == key for t in glossary for s in [t.text, *t.aliases])


def _sid(kind: str, wrong: str, right: str) -> str:
    return hashlib.sha1(f"{kind}|{wrong.lower()}|{right}".encode()).hexdigest()[:10]


@dataclass
class Suggestion:
    id: str
    kind: str                   # "term" | "rule"
    right: str
    wrong: str = ""             # chỉ với rule
    topic: str | None = None    # chủ đề gợi ý cho term (chủ đề hay gặp nhất trong các lượt)
    turn_ids: list[str] = field(default_factory=list)
    examples: list[dict] = field(default_factory=list)

    @property
    def warning(self) -> str | None:
        # "interface" → "integrate": rule sẽ đổi cả những lần bạn nói "interface" thật
        if self.kind == "rule" and all(_is_latin(w) for w in self.wrong.split()):
            return (f"Rule sẽ đổi MỌI chỗ STT viết \"{self.wrong}\", kể cả khi bạn nói từ đó thật. "
                    f"Nếu có lúc bạn nói \"{self.wrong}\" thì bỏ rule này, chỉ duyệt thuật ngữ \"{self.right}\".")
        return None

    def to_dict(self) -> dict:
        return {**asdict(self), "warning": self.warning}


def suggest(turns: list[Turn], glossary: list[Term], rules: list[tuple[str, str]],
            decided: set[str], min_turns: int = MIN_TURNS) -> list[Suggestion]:
    terms: dict[str, list[Turn]] = defaultdict(list)
    pairs: dict[tuple[str, str], list[Turn]] = defaultdict(list)
    for t in turns:
        if t.fixed is None or t.split != "dev":
            continue
        seen_terms, seen_pairs = set(), set()
        fixed = " ".join(w for tok in t.fixed.split() for w in _unglue(tok))
        for wrong, right in diff_spans(t.text, fixed):
            # Chỉ học thuật ngữ. Lỗi giữa các từ tiếng Việt ("với" → "giúp") mà thành rule
            # thì sẽ phá mọi câu khác có từ đó.
            phrases = latin_phrases(right)
            if not phrases:
                continue
            for p in phrases:
                if not in_glossary(p, glossary) and p.lower() not in seen_terms:
                    seen_terms.add(p.lower())
                    terms[p].append(t)
            key = (wrong.lower(), right)
            if wrong and key not in seen_pairs:
                seen_pairs.add(key)
                pairs[key].append(t)

    existing = {src.lower() for src, _ in rules}
    out: list[Suggestion] = []
    for phrase, ts in terms.items():
        sid = _sid("term", "", phrase)
        if len(ts) >= min_turns and sid not in decided:
            topic = Counter(t.topic for t in ts if t.topic).most_common(1)
            out.append(Suggestion(sid, "term", phrase, topic=topic[0][0] if topic else None,
                                  turn_ids=[t.turn_id for t in ts], examples=_examples(ts)))
    for (wrong, right), ts in pairs.items():
        sid = _sid("rule", wrong, right)
        # Phần sai chứa thuật ngữ đúng của glossary ("interface") thì rule sẽ sửa hỏng chỗ dùng đúng
        wrong_norm = normalize(wrong)
        hits_term = any(contains_term(wrong_norm, s) for g in glossary for s in [g.text, *g.aliases])
        if len(ts) >= min_turns and sid not in decided and wrong not in existing and not hits_term:
            out.append(Suggestion(sid, "rule", right, wrong=_original_case(wrong, ts),
                                  turn_ids=[t.turn_id for t in ts], examples=_examples(ts)))
    return sorted(out, key=lambda s: (-len(s.turn_ids), s.kind, s.right.lower()))


def _original_case(wrong_lower: str, ts: list[Turn]) -> str:
    m = re.search(rf"(?<!\w){re.escape(wrong_lower)}(?!\w)", ts[0].text, re.I)
    return m.group(0) if m else wrong_lower


def _examples(ts: list[Turn], n: int = 3) -> list[dict]:
    return [{"turn_id": t.turn_id, "heard": t.text, "fixed": t.fixed} for t in ts[:n]]


# ---------------------------------------------------------------- kiểm tra


def _wer(ref: str, hyp: str) -> float:
    ref, hyp = normalize(ref), normalize(hyp)
    if not ref:
        return 0.0 if not hyp else 1.0
    return jiwer.wer(ref, hyp or "<empty>")


def _testset_items() -> list[dict]:
    manifest = TESTSET / "transcripts.jsonl"
    if not manifest.exists():
        return []
    return [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()]


def _score(pairs: list[tuple[str, str, list[str]]]) -> dict:
    """pairs: (ref, hyp, terms) → WER corpus + số thuật ngữ đúng."""
    if not pairs:
        return {"n": 0, "wer": None, "terms_hit": 0, "terms": 0}
    refs = [normalize(r) or "<empty>" for r, _, _ in pairs]
    hyps = [normalize(h) or "<empty>" for _, h, _ in pairs]
    hit = sum(contains_term(normalize(h), term) for _, h, terms in pairs for term in terms)
    return {"n": len(pairs), "wer": round(jiwer.wer(refs, hyps), 4), "terms_hit": hit,
            "terms": sum(len(t) for _, _, t in pairs)}


def _not_worse(before: dict, after: dict, wer_tol: float = 0.0) -> bool:
    if not before["n"]:
        return True
    return after["wer"] <= before["wer"] + wer_tol and after["terms_hit"] >= before["terms_hit"]


def check_rule(s: Suggestion, turns: list[Turn], rules: list[tuple[str, str]]) -> dict:
    """Áp rule vào transcript thô có sẵn, không cần GPU."""
    old, new = Corrector(rules), Corrector([*rules, (s.wrong, s.right)])

    # Lượt của người dùng: rule đổi kết quả ở đâu, gần với bản đúng hơn hay xa hơn
    effect = {"dev": {"better": 0, "worse": 0}, "holdout": {"better": 0, "worse": 0}}
    worse_examples = []
    for t in turns:
        before, after = old.apply(t.raw), new.apply(t.raw)
        if before == after:
            continue
        d = _wer(t.final, after) - _wer(t.final, before)
        if d < 0:
            effect[t.split]["better"] += 1
        elif d > 0:
            effect[t.split]["worse"] += 1
            worse_examples.append({"turn_id": t.turn_id, "before": before, "after": after})

    # Bộ test Phase 1: chấm lại transcript thô của cấu hình đang dùng
    testset = {"available": BASELINE_RUN.exists()}
    if testset["available"]:
        terms = {it["id"]: it["terms"] for it in _testset_items()}
        with open(BASELINE_RUN, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        testset["before"] = _score([(r["ref"], old.apply(r["raw"]), terms.get(r["id"], [])) for r in rows])
        testset["after"] = _score([(r["ref"], new.apply(r["raw"]), terms.get(r["id"], [])) for r in rows])

    worse = effect["dev"]["worse"] + effect["holdout"]["worse"]
    passed = worse == 0 and (not testset["available"] or _not_worse(testset["before"], testset["after"]))
    return {"passed": passed, "turns": effect, "worse_examples": worse_examples[:3], "testset": testset,
            "reason": None if passed else ("làm sai lượt nói khác của bạn" if worse else "làm giảm điểm bộ test")}


def check_term(s: Suggestion, topic: str, stt, turns: list[Turn]) -> dict:
    """Chạy lại STT với glossary đã thêm thuật ngữ (GPU, vài chục giây).

    stt: SttService. Gọi trong thread, đã giữ khoá GPU.
    """
    import soundfile as sf

    glossary = stt.glossary()
    new_glossary = [*glossary, Term(s.right, False, [], topic)]
    prompt = build_prompt(new_glossary, "topic", topic) or ""
    tokenizer = getattr(getattr(stt.engine, "model", None), "hf_tokenizer", None)
    tokens = len(tokenizer.encode(" " + prompt).ids) if tokenizer else None

    # Lượt của người dùng có thuật ngữ này: giờ có nghe ra đúng không
    own = {"dev": [0, 0], "holdout": [0, 0]}  # [đúng, tổng]
    for t in turns:
        if t.audio is None or not t.audio.exists() or not contains_term(normalize(t.final), s.right):
            continue
        audio, _ = sf.read(t.audio, dtype="float32")
        out = stt.transcribe(audio, topic, glossary=new_glossary).text
        own[t.split][0] += contains_term(normalize(out), s.right)
        own[t.split][1] += 1

    # Bộ test Phase 1, các câu cùng chủ đề: thêm thuật ngữ có làm hỏng câu khác không
    before, after = [], []
    for it in _testset_items():
        if detect_topic(it["text"], glossary) != topic:
            continue
        audio, _ = sf.read(TESTSET / it["file"], dtype="float32")
        before.append((it["text"], stt.transcribe(audio, topic, glossary=glossary).text, it["terms"]))
        after.append((it["text"], stt.transcribe(audio, topic, glossary=new_glossary).text, it["terms"]))
    testset = {"available": bool(before), "before": _score(before), "after": _score(after)}

    # Nhận dạng có chút dao động giữa các lần prompt khác nhau: cho WER lệch 0.5 điểm
    passed = _not_worse(testset["before"], testset["after"], wer_tol=0.005)
    return {"passed": passed, "topic": topic, "prompt_tokens": tokens,
            "prompt_cut": tokens is not None and tokens > PROMPT_TOKEN_LIMIT,
            "own": own, "testset": testset, "reason": None if passed else "làm giảm điểm bộ test"}


# ---------------------------------------------------------------- duyệt


def decided_ids(learn_dir: Path) -> set[str]:
    path = learn_dir / "decisions.jsonl"
    if not path.exists():
        return set()
    return {json.loads(line)["id"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def record(learn_dir: Path, s: Suggestion, decision: str, **extra):
    learn_dir.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "decision": decision, **s.to_dict(), **extra}
    with open(learn_dir / "decisions.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def apply(s: Suggestion, topic: str | None, glossary_path: Path, corrections_path: Path):
    """Ghi đề xuất đã duyệt vào data/. Server tự đọc lại file khi đổi."""
    if s.kind == "term":
        if not topic:
            raise ValueError("Cần chọn chủ đề cho thuật ngữ")
        add_term(s.right, topic, glossary_path)
    else:
        text = corrections_path.read_text(encoding="utf-8") if corrections_path.exists() else ""
        if text and not text.endswith("\n"):
            text += "\n"
        corrections_path.write_text(f"{text}{s.wrong}\t{s.right}\n", encoding="utf-8")
