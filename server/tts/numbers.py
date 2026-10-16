"""Đọc số bằng tiếng Việt: 21 → "hai mươi mốt", 1005 → "một nghìn không trăm linh năm"."""

from __future__ import annotations

DIGITS = "không một hai ba bốn năm sáu bảy tám chín".split()
_SCALES = ["", "nghìn", "triệu"]


def read_digits(s: str) -> str:
    """Đọc từng chữ số: "05" → "không năm"."""
    return " ".join(DIGITS[int(c)] for c in s if c.isdigit())


def _group(g: int, full: bool) -> list[str]:
    """Một nhóm 3 chữ số. full: nhóm nằm giữa số nên phải đọc cả "không trăm", "linh"."""
    h, t, u = g // 100, g // 10 % 10, g % 10
    w = []
    if h or full:
        w += [DIGITS[h], "trăm"]
    if t == 0:
        if u:
            if h or full:
                w.append("linh")
            w.append(DIGITS[u])
    elif t == 1:
        w.append("mười")
        if u == 5:
            w.append("lăm")
        elif u:
            w.append(DIGITS[u])
    else:
        w += [DIGITS[t], "mươi"]
        w += [{1: "mốt", 4: "tư", 5: "lăm"}.get(u, DIGITS[u])] if u else []
    return w


def read_int(n: int) -> str:
    if n == 0:
        return "không"
    if n < 0:
        return "âm " + read_int(-n)
    groups = []
    while n:
        groups.append(n % 1000)
        n //= 1000
    top = len(groups) - 1
    words = []
    for i in range(top, -1, -1):
        if groups[i] == 0:
            # Nhóm "tỷ" bằng 0 nhưng phía trên còn số: "một nghìn tỷ" vẫn cần chữ "tỷ"
            if i % 3 == 0 and i and words and words[-1] != "tỷ":
                words.append("tỷ")
            continue
        words += _group(groups[i], full=i < top)
        scale = " ".join(filter(None, [_SCALES[i % 3]] + ["tỷ"] * (i // 3)))
        if scale:
            words += scale.split()
    return " ".join(words)


def read_number(s: str) -> str:
    """Số dạng text, đã bỏ dấu phân cách nghìn. Số dài quá 15 chữ số (mã, số điện thoại) thì đọc từng chữ số."""
    s = s.lstrip("+")
    if len(s) > 15:
        return read_digits(s)
    return read_int(int(s))


def read_decimal(int_part: str, frac: str) -> str:
    """3,14 → "ba phẩy mười bốn"; 0,05 → "không phẩy không năm"."""
    tail = read_digits(frac) if frac.startswith("0") or len(frac) > 2 else read_int(int(frac))
    return f"{read_number(int_part)} phẩy {tail}"


def read_version(v: str) -> str:
    """3.11.2 → "ba chấm mười một chấm hai"."""
    return " chấm ".join(read_number(p) if len(p) < 4 or not p.startswith("0") else read_digits(p)
                         for p in v.split("."))
