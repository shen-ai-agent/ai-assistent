import unicodedata

from server.stt.corrections import Corrector
from server.stt.textnorm import contains_term, normalize


def test_dotnet_variants():
    assert normalize("Học .NET") == normalize("học dotnet") == normalize("học dot net") == "học dotnet"
    assert normalize("ASP.NET Core") == "asp dotnet core"


def test_csharp():
    assert normalize("C#") == normalize("c sharp") == "c sharp"


def test_punct_and_separators():
    assert normalize("X-Ray, CI/CD?") == "x ray ci cd"
    assert normalize("I'd like it.") == "id like it"


def test_term_ignores_spaces():
    assert contains_term(normalize("dùng EC 2 và S 3"), "EC2")
    assert contains_term(normalize("dùng EC2"), "EC 2")
    assert contains_term(normalize("TOEIC Part5"), "Part 5")
    assert contains_term(normalize("một CICD pipeline"), "CI/CD")
    assert contains_term(normalize("với Private Link"), "PrivateLink")
    assert contains_term(normalize("since 3 years"), "since")
    assert not contains_term(normalize("Part 50"), "Part 5")


def test_english_numbers():
    assert normalize("since three years") == normalize("since 3 years") == "since 3 years"
    assert normalize("ba năm") == "ba năm"


def test_vietnamese_nfc():
    decomposed = "Tiếng Việt"
    assert normalize(decomposed) == "tiếng việt"


def test_contains_term_word_boundary():
    hyp = normalize("Cái NAT Gateway này khác Internet Gateway")
    assert contains_term(hyp, "NAT Gateway")
    assert contains_term(hyp, "internet gateway")
    assert not contains_term(normalize("dùng S3 bucket"), "S")
    assert not contains_term(normalize("ECS cluster"), "EC")


def test_corrector():
    c = Corrector([("cloud front", "CloudFront"), ("lam đa", "Lambda")])
    assert c.apply("Dùng Cloud Front với lam đa") == "Dùng CloudFront với Lambda"
    assert c.apply("cloud frontier") == "cloud frontier"
