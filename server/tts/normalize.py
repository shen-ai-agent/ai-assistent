"""Biến câu trả lời của Claude thành text đọc được cho TTS.

  clean_structure   cắt phần sau ---CODE---, code block, markdown, link, URL, đường dẫn (cả đoạn)
  split_sentences   tách câu (splitter.py)
  speakable         từng câu: viết tắt, lexicon, số, ký hiệu

Lexicon chạy trước bước đọc số; nếu không, "EC2" sẽ thành "E C hai" trước khi kịp tra lexicon.
Câu tiếng Anh hoàn toàn giữ số và thuật ngữ cho model đọc theo kiểu tiếng Anh.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .numbers import read_decimal, read_digits, read_number, read_version
from .splitter import split_sentences

ROOT = Path(__file__).resolve().parents[2]
LEXICON_PATH = ROOT / "data" / "lexicon.tsv"
CODE_MARKER = "---CODE---"
CODE_SPOKEN = "đoạn code trên màn hình"

# Tên chữ cái theo cách dân dev Việt hay đọc
LETTERS = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", [
    "ây", "bi", "xi", "đi", "i", "ép", "gi", "ếch", "ai", "giây", "kây", "eo", "em", "en", "ô", "pi",
    "kiu", "a", "ét", "ti", "iu", "vi", "đắp bờ liu", "ích", "oai", "dét"]))

_VI_CHARS = re.compile(r"[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]", re.I)

_EN_FUNCTION = set("""a an the i you he she we they it is are was were be been am have has had do does did
to of in on at for with from by and or but not this that these those my your our their can could will would
should there what which who how if so""".split())

ABBREV_VI = {"v.v.": "vân vân", "vv.": "vân vân", "e.g.": "ví dụ", "i.e.": "tức là", "etc.": "vân vân",
             "vs.": "so với", "vs": "so với", "VD:": "ví dụ:"}
ABBREV_EN = {"e.g.": "for example", "i.e.": "that is", "etc.": "et cetera", "vs.": "versus", "vs": "versus"}

UNITS = {
    "%": "phần trăm", "GB": "gigabyte", "MB": "megabyte", "KB": "kilobyte", "TB": "terabyte",
    "GiB": "gigabyte", "MiB": "megabyte", "ms": "mili giây", "s": "giây", "h": "giờ", "GHz": "gigahertz",
    "MHz": "megahertz", "Hz": "hertz", "k": "nghìn", "K": "nghìn", "x": "lần", "km": "ki lô mét",
    "kg": "ki lô gam", "°C": "độ C", "USD": "đô la", "đ": "đồng", "VND": "đồng",
}
FILE_EXT = "py|js|ts|tsx|jsx|cs|csproj|sln|json|md|txt|toml|ya?ml|html|css|sh|ps1|tsv|csv|wav|mp3|env|sql|xml|config"


@dataclass
class Segment:
    text: str   # đã chuẩn hoá, đưa thẳng vào TTS
    lang: str   # "vi" | "en"
    source: str = ""  # câu trước khi chuẩn hoá (đã bỏ markdown), để hiển thị


# ---------------------------------------------------------------- lexicon


@dataclass
class Lexicon:
    entries: list[tuple[str, str, bool]]  # (thuật ngữ, cách đọc, là phiên âm từ tiếng Anh)

    @classmethod
    def load(cls, path: Path = LEXICON_PATH) -> "Lexicon":
        entries = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip() or line.startswith("#") or "\t" not in line:
                    continue
                term, reading = line.split("\t", 1)
                loan = term.startswith("~")
                entries.append((term.lstrip("~").strip(), reading.strip(), loan))
        return cls(entries)

    def pattern(self, loanwords: bool) -> tuple[re.Pattern | None, dict[str, str]]:
        """Một regex cho cả lexicon, cụm dài khớp trước.

        Viết tắt toàn chữ hoa ("AI", "IP") khớp đúng hoa thường, vì "ai" là từ tiếng Việt.
        """
        items = sorted(((t, r) for t, r, loan in self.entries if loanwords or not loan), key=lambda x: -len(x[0]))
        if not items:
            return None, {}
        parts = []
        for term, _ in items:
            esc = re.escape(term)
            parts.append(esc if term.upper() == term else f"(?i:{esc})")
        lookup = {}
        for term, reading in items:
            lookup.setdefault(term if term.upper() == term else term.lower(), reading)
        return re.compile(rf"(?<![\w.])(?:{'|'.join(parts)})(?![\w#+])"), lookup


_DEFAULT_LEXICON: Lexicon | None = None


def default_lexicon() -> Lexicon:
    global _DEFAULT_LEXICON
    if _DEFAULT_LEXICON is None:
        _DEFAULT_LEXICON = Lexicon.load()
    return _DEFAULT_LEXICON


# ---------------------------------------------------------------- cả đoạn


def _inline_code(m: re.Match) -> str:
    code = m.group(1).strip()
    # Tên ngắn (biến, lệnh, file) thì đọc; biểu thức có ngoặc, dấu bằng thì bỏ
    if len(code) <= 30 and not re.search(r"[(){};=<>\[\]\"']", code):
        return code
    return "đoạn code"


def _url(m: re.Match) -> str:
    url = m.group(0).rstrip(".,;:!?)")
    tail = m.group(0)[len(url):]
    parsed = urlparse(url if "://" in url else "http://" + url)
    host = (parsed.hostname or "").removeprefix("www.")
    port = f" cổng {read_digits(str(parsed.port))}" if parsed.port else ""
    return f"trang {host.replace('.', ' chấm ')}{port}{tail}" if host else tail


def _table_row(line: str) -> str:
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return ", ".join(c for c in cells if c) + "."


def clean_structure(text: str) -> str:
    """Bỏ phần không đọc được (code, markdown, URL). Trả về văn xuôi, mỗi dòng cũ thành một câu."""
    text = text.split(CODE_MARKER, 1)[0]
    text = re.sub(r"```.*?(?:```|$)", f"\n{CODE_SPOKEN}.\n", text, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", _inline_code, text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)                # ảnh
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)              # [chữ](link) → chữ
    text = re.sub(r"</?[a-zA-Z][^>\n]*>", " ", text)                  # thẻ HTML
    text = re.sub(r"https?://\S+|www\.\S+", _url, text)
    text = re.sub(r"(?<![\w])[A-Za-z]:\\[^\s,;]*", "đường dẫn", text)  # C:\Users\...
    text = re.sub(r"(?<![\w.~])(?:~|\.{1,2})?(?:/[\w.@-]+){2,}/?", "đường dẫn", text)  # /etc/nginx/x.conf

    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s or re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?", s):  # dòng kẻ của bảng
            continue
        if re.fullmatch(r"([-*_]\s*){3,}", s):                        # đường kẻ ngang
            continue
        if s.startswith("|") and s.endswith("|"):
            s = _table_row(s)
        s = re.sub(r"^#{1,6}\s+", "", s)                               # heading
        s = re.sub(r"^(>\s*)+", "", s)                                 # trích dẫn
        s = re.sub(r"^([-*+•]|\d+[.)])\s+", "", s)                     # gạch đầu dòng, danh sách số
        s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)                      # đậm
        s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", s)  # nghiêng *x*
        s = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"\1", s)     # nghiêng _x_ (không đụng snake_case)
        s = re.sub(r"~~(.+?)~~", r"\1", s)
        s = s.strip()
        if s and not re.search(r"[.!?;:…]$", s):
            s += "."  # xuống dòng là một chỗ ngắt
        if s:
            lines.append(s)
    return " ".join(lines)


# ---------------------------------------------------------------- từng câu


def detect_lang(sentence: str) -> str:
    """Câu có chữ tiếng Việt có dấu → vi. Không dấu và ≥3 từ tiếng Anh → en."""
    if _VI_CHARS.search(sentence):
        return "vi"
    words = re.findall(r"[A-Za-z][A-Za-z']*", sentence)
    # "Lambda: cold start 200ms" là ghi chú kỹ thuật, không phải câu tiếng Anh
    return "en" if len(words) >= 4 and any(w.lower() in _EN_FUNCTION for w in words) else "vi"


def _replace_abbrev(s: str, table: dict[str, str]) -> str:
    for k, v in table.items():
        # "v.v." cuối câu: giữ dấu chấm để câu sau không bị đọc dính vào
        end = "." if k.endswith(".") else ""
        s = re.sub(rf"(?<![\w.]){re.escape(k)}(?=\s+[A-ZÀ-Ỹ]|$)", v + end, s)
        s = re.sub(rf"(?<![\w.]){re.escape(k)}(?!\w)", v, s)
    return s


def _spell(letters: str) -> str:
    return " ".join(LETTERS[c] for c in letters)


_NUM = r"[1-9]\d{0,2}(?:[.,]\d{3})+(?![\d])|\d+[.,]\d+|\d+"
_UNIT = "|".join(re.escape(u) for u in sorted(UNITS, key=len, reverse=True))


def _read_num_token(tok: str) -> str:
    if re.fullmatch(r"[1-9]\d{0,2}(?:[.,]\d{3})+", tok):
        return read_number(re.sub(r"[.,]", "", tok))  # 1.500 / 1,500 → nghìn
    m = re.fullmatch(r"(\d+)[.,](\d+)", tok)
    if m:
        return read_decimal(m.group(1), m.group(2))
    return read_number(tok)


def _numbers_vi(s: str) -> str:
    # Ngày, giờ
    s = re.sub(r"(?<!ngày )\b(\d{1,2})/(\d{1,2})/(\d{4})\b", lambda m: f"ngày {m[0]}", s)
    s = re.sub(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b",
               lambda m: f"{read_number(m[1])} tháng {read_number(m[2])} năm {read_number(m[3])}", s)
    s = re.sub(r"\b([01]?\d|2[0-3]):([0-5]\d)\b",
               lambda m: f"{read_number(m[1])} giờ" + (f" {read_number(m[2])}" if m[2] != "00" else ""), s)
    # Điểm số, CIDR: 420/495 → "trên"; 900+ → "trở lên"
    s = re.sub(r"(?<=\d)\s?/\s?(?=\d)", " trên ", s)
    s = re.sub(r"(?<=\d)\+(?!\w)", " trở lên", s)
    # Phiên bản: v3.11, Python 3.11, 1.2.3
    s = re.sub(r"(?<![\w.])[vV](\d+(?:\.\d+)*)\b", lambda m: "vê " + read_version(m[1]), s)
    # 1.200.000 là số có phân cách nghìn, không phải phiên bản
    s = re.sub(r"\b(?![1-9]\d{0,2}(?:\.\d{3})+\b)(\d+\.\d+\.\d+(?:\.\d+)*)\b", lambda m: read_version(m[1]), s)
    # Tiền đô, khoảng số
    s = re.sub(rf"\$\s?({_NUM})", lambda m: f"{_read_num_token(m[1])} đô", s)
    s = re.sub(rf"({_NUM})\s?[-–]\s?({_NUM})(?=\s?(?:{_UNIT})?(?!\w))",
               lambda m: f"{m[1]} đến {m[2]}", s)
    # Số kèm đơn vị, rồi số trơn
    s = re.sub(rf"(?<![\w.])({_NUM})\s?({_UNIT})(?![\w])",
               lambda m: f"{_read_num_token(m[1])} {UNITS[m[2]]}", s)
    s = re.sub(rf"(?<![\w])({_NUM})(?![\w])", lambda m: _read_num_token(m[1]), s)
    # Số còn dính chữ (sau khi đã tra lexicon): T3 → "ti ba"
    s = re.sub(r"\d+", lambda m: f" {read_number(m[0])} ", s)
    return s


def _symbols(s: str, lang: str) -> str:
    vi = lang == "vi"
    for pat, rep in [(r"\s*(?:->|=>|→)\s*", " sang " if vi else " to "), (r"\s*&\s*", " và " if vi else " and "),
                     (r"\s+=\s+", " bằng " if vi else " equals "), (r"\s+\+\s+", " cộng " if vi else " plus "),
                     (r"@", " a còng " if vi else " at "), (r"%", " phần trăm " if vi else " percent ")]:
        s = re.sub(pat, rep, s)
    s = re.sub(r"\s[-–—]\s", ", ", s)                          # gạch ngang giữa câu
    s = re.sub(r"\s*[()\[\]{}]\s*", ", ", s)                   # ngoặc → ngắt nhẹ
    s = re.sub(r"(?<=\w)[-/](?=\w)", " ", s)                    # event-driven, dev/holdout
    s = re.sub(r"[\"“”‘’`*_#~^|\\<>]", " ", s)
    s = re.sub(r"(?<!\w)'|'(?!\w)", " ", s)                      # nháy đơn, giữ "don't"
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+([,.!?;:…])", r"\1", s)
    s = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", s)               # ", ," sau khi bỏ ngoặc
    s = re.sub(r"^[\s,;:.]+", "", s)
    s = re.sub(r",\s*([.!?…])", r"\1", s)
    return s.strip()


def speakable(sentence: str, lang: str = "vi", lexicon: Lexicon | None = None, *,
              loanwords: bool = False, spell_acronyms: bool = True) -> str:
    """Một câu → text đưa vào TTS.

    loanwords:      dùng cả phiên âm từ tiếng Anh trong lexicon (dòng ~), cho model đọc tiếng Anh dở.
    spell_acronyms: viết tắt không có trong lexicon thì đánh vần ("VPN" → "vi pi en").
    """
    s = sentence
    if lang == "en":
        return _symbols(_replace_abbrev(s, ABBREV_EN), "en")

    s = _replace_abbrev(s, ABBREV_VI)
    # Phiên bản trước lexicon: "Python 3.11" phải đọc "chấm", nhưng sau lexicon chữ hoa đã mất
    s = re.sub(rf"(?<![\w.])([A-Z][A-Za-z.#+]*\s+)(\d+\.\d+)(?![\d.])(?!\s?(?:{_UNIT})(?!\w))",
               lambda m: m[1] + read_version(m[2]), s)
    # "đô/GB", "$0.5/giờ" → "mỗi gigabyte", "mỗi giờ" (trước bước đánh vần, nếu không "GB" thành "gi bi")
    s = re.sub(rf"(?<=[\w%$])\s?/\s?({_UNIT}|giờ|ngày|tháng|năm|người|request|user)(?!\w)",
               lambda m: " mỗi " + UNITS.get(m[1], m[1]), s)
    pat, lookup = (lexicon or default_lexicon()).pattern(loanwords)
    if pat:
        s = pat.sub(lambda m: f" {lookup.get(m[0]) or lookup[m[0].lower()]} ", s)
    s = re.sub(rf"(?<![\w.])([\w-]+)\.({FILE_EXT})\b", r"\1 chấm \2", s)  # app.py → app chấm py
    s = re.sub(r"(?<=[A-Za-z0-9])\.(?=[a-z])|(?<!\S)\.(?=[A-Za-z])", " chấm ", s)  # t3.micro, .Result
    if spell_acronyms:
        s = re.sub(r"(?<![\w.])([A-Z]{2,6})(?=\d|[^\w]|$)", lambda m: _spell(m[1]), s)
        s = re.sub(r"(?<![\w.])([A-Z])(?=\d)", lambda m: _spell(m[1]) + " ", s)
    s = _numbers_vi(s)
    return _symbols(s, "vi")


def _split_lang(sents: list[str]) -> list[str]:
    """Tách ở dấu hai chấm / ngoặc kép khi hai bên khác ngôn ngữ: "Câu mẫu là: I have been..." """
    out = []
    for s in sents:
        m = re.search(r'(?<=:)\s+(?=\S)|\s+(?=["“])', s)
        if m and detect_lang(s[:m.start()]) != detect_lang(s[m.end():]):
            out += [s[:m.start()].strip(), s[m.end():].strip()]
        else:
            out.append(s)
    return out


def prepare(text: str, lexicon: Lexicon | None = None, **opts) -> list[Segment]:
    """Câu trả lời → danh sách câu đọc được, theo thứ tự, kèm ngôn ngữ."""
    out = []
    for sent in _split_lang(split_sentences(clean_structure(text), key=detect_lang)):
        lang = detect_lang(sent)
        spoken = speakable(sent, lang, lexicon, **opts)
        if re.search(r"\w", spoken):
            out.append(Segment(spoken, lang, sent))
    return out
