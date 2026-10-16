"""Chuẩn hoá text cho TTS: đọc số, tách câu, thuật ngữ, markdown."""

import pytest

from server.tts.normalize import Lexicon, clean_structure, detect_lang, prepare, speakable
from server.tts.numbers import read_decimal, read_int, read_version
from server.tts.splitter import split_sentences


# ---------- numbers ----------

@pytest.mark.parametrize("n, text", [
    (0, "không"),
    (5, "năm"),
    (10, "mười"),
    (15, "mười lăm"),
    (21, "hai mươi mốt"),
    (24, "hai mươi tư"),
    (25, "hai mươi lăm"),
    (105, "một trăm linh năm"),
    (1005, "một nghìn không trăm linh năm"),
    (2_000_005, "hai triệu không trăm linh năm"),
    (1_000_000_000_000, "một nghìn tỷ"),
])
def test_read_int(n, text):
    assert read_int(n) == text


def test_read_decimal_and_version():
    assert read_decimal("3", "14") == "ba phẩy mười bốn"
    assert read_decimal("0", "05") == "không phẩy không năm"
    assert read_version("3.11.2") == "ba chấm mười một chấm hai"


# ---------- splitter ----------

def test_split_keeps_decimal_and_filename():
    sents = split_sentences("Mở file app.py bằng Python 3.11 nhé bạn. Sau đó chạy lệnh test cho chắc.")
    assert sents == ["Mở file app.py bằng Python 3.11 nhé bạn.", "Sau đó chạy lệnh test cho chắc."]


def test_split_merges_short_sentences():
    assert split_sentences("Đúng. Bạn làm rất tốt rồi đấy, tiếp tục nhé.") == \
        ["Đúng. Bạn làm rất tốt rồi đấy, tiếp tục nhé."]


def test_split_abbrev_midsentence_and_at_end():
    assert len(split_sentences("Ví dụ e.g. EC2 và S3 đều là dịch vụ AWS phổ biến.")) == 1
    assert split_sentences("Có EC2, S3, RDS v.v. Bạn nên học IAM trước nhé.") == \
        ["Có EC2, S3, RDS v.v.", "Bạn nên học IAM trước nhé."]


def test_split_cuts_long_first_sentence_at_comma():
    text = ("Khi thiết kế hệ thống trên AWS bạn cần nghĩ tới tính sẵn sàng cao, "
            "chi phí vận hành hằng tháng và khả năng mở rộng khi lượng người dùng tăng đột biến.")
    first, *rest = split_sentences(text)
    assert first.endswith("cao,") and rest


def test_split_does_not_merge_different_lang():
    sents = split_sentences("Đúng rồi. Good job.", key=detect_lang)
    assert len(sents) == 1 or detect_lang(sents[0]) == detect_lang(sents[-1])


# ---------- detect_lang ----------

@pytest.mark.parametrize("s, lang", [
    ("Tôi đang học AWS.", "vi"),
    ("I have been working as a developer.", "en"),
    ("Lambda: cold start 200-500ms.", "vi"),   # ghi chú kỹ thuật, không phải câu tiếng Anh
    ("EC2 instance type t3.micro", "vi"),
])
def test_detect_lang(s, lang):
    assert detect_lang(s) == lang


# ---------- speakable ----------

def say(s, **kw):
    return speakable(s, "vi", **kw)


def test_lexicon_before_numbers():
    assert "i xi tu" in say("Chạy EC2 nhé.")
    assert "ét ba" in say("Lưu vào S3.")
    assert "hai" not in say("Lưu vào S3.")


def test_acronym_case_sensitive():
    out = say("AI giúp ai đó học nhanh hơn.")
    assert out.startswith("ây ai giúp ai đó")


def test_unknown_acronym_spelled():
    assert "vi pi en" in say("Dùng VPN và SSO.")


def test_loanwords_only_when_enabled():
    assert "Lambda" in say("Gọi Lambda.")
    assert "lam đa" in say("Gọi Lambda.", loanwords=True)


def test_spell_acronyms_off():
    assert "VPN" in say("Dùng VPN.", spell_acronyms=False)


def test_number_with_unit():
    out = say("EC2 tốn khoảng 12GB RAM.")
    assert "mười hai gigabyte" in out and "ram" in out


def test_price_per_unit():
    assert "không phẩy năm đô mỗi giờ" in say("Giá $0.5/giờ.")
    assert "không phẩy không hai ba đô mỗi gigabyte" in say("Giá 0,023 đô/GB.")


def test_thousands_separator():
    assert "một triệu hai trăm nghìn" in say("Có 1.200.000 request.")


def test_range():
    assert "hai trăm đến năm trăm mili giây" in say("Cold start 200-500ms.")


def test_percent():
    assert "hai mươi lăm phần trăm" in say("Nhanh hơn 25%.")


def test_version():
    assert "vê hai chấm ba chấm một" in say("Phiên bản v2.3.1.")
    assert "Python ba chấm mười một" in say("Python 3.11 nhanh hơn.")


def test_date_and_time():
    assert "ngày năm tháng ba năm hai nghìn không trăm hai mươi lăm" in say("Ngày 05/03/2025.")
    assert "ngày ngày" not in say("Ra ngày 05/03/2025.")
    assert "chín giờ ba mươi" in say("Họp lúc 9:30.")


def test_filename():
    assert "app chấm py" in say("Mở file app.py.")


def test_symbols():
    assert "sang" in say("Request -> Lambda.")
    assert "và" in say("Q&A")
    assert "(" not in say("Dùng S3 (lưu trữ).")


def test_abbrev():
    assert "vân vân" in say("EC2, S3 v.v.")


def test_english_sentence_untouched():
    assert speakable("I have been working for 3 years.", "en") == "I have been working for 3 years."


def test_custom_lexicon(tmp_path):
    p = tmp_path / "lex.tsv"
    p.write_text("# chú thích\nFooBar\tphu ba\n~Baz\tbát\n", encoding="utf-8")
    lex = Lexicon.load(p)
    assert "phu ba" in speakable("Dùng FooBar.", "vi", lex)
    assert "Baz" in speakable("Dùng Baz.", "vi", lex)


# ---------- clean_structure / prepare ----------

def test_code_marker_cut():
    assert "print" not in clean_structure("Xem code bên dưới.\n---CODE---\nprint(1)")


def test_fenced_code_replaced():
    out = clean_structure("Ví dụ:\n```python\nprint(1)\n```\nXong.")
    assert "print" not in out and "đoạn code" in out


def test_markdown_stripped():
    out = clean_structure("## Tiêu đề\n- **Đậm** và *nghiêng*\n> trích dẫn")
    assert not set("#*>") & set(out)


def test_url_and_path():
    assert "trang docs chấm aws" in clean_structure("Xem https://docs.aws.amazon.com/lambda/ nhé.")
    assert "đường dẫn" in clean_structure(r"Mở C:\Users\me\app.py nhé.")


def test_table_row():
    segs = prepare("| Dịch vụ | Giá |\n|---|---|\n| S3 | 0,023 đô/GB |")
    text = " ".join(s.text for s in segs)
    assert "|" not in text and "---" not in text and "ét ba" in text


def test_prepare_splits_english_quote():
    segs = prepare("Câu mẫu Part 2 có thể là: I have been working as a developer for 3 years.")
    assert [s.lang for s in segs] == ["vi", "en"]
    assert "3 years" in segs[1].text


def test_prepare_no_empty_segments():
    segs = prepare("```\ncode\n```\n\n---\n\nXong rồi.")
    assert all(s.text.strip() for s in segs)


def test_score_cidr_plus():
    assert "bốn trăm hai mươi trên bốn trăm chín mươi lăm" in say("Listening 420/495.")
    assert "chín trăm trở lên" in say("Mục tiêu 900+ điểm.")
    assert "không trên mười sáu" in say("CIDR 10.0.0.0/16.")


def test_dotted_identifier():
    assert "chấm micro" in say("Dùng t3.micro.")
    assert "gọi chấm Result" in say("Đừng gọi .Result nhé.")


def test_url_port():
    assert "localhost cổng năm không không một" in clean_structure("Mở https://localhost:5001/swagger nhé.")


def test_abbrev_at_sentence_end_keeps_period():
    assert "vân vân. Bạn" in say("EC2, S3 v.v. Bạn nhớ nhé.")
