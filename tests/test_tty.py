import pytest

from kanta.tty import ESC, Colors, Line, colors, displaywidth, pad, strip_ansi


def test_strip_ansi():
    assert strip_ansi(f"{ESC}1;34mhello{ESC}0m") == "hello"


def test_displaywidth_plain_and_ansi():
    assert displaywidth("hello") == 5
    assert displaywidth(f"{ESC}38;5;226mhi{ESC}0m") == 2


def test_displaywidth_wide_and_combining_chars():
    assert displaywidth("你好") == 4
    assert displaywidth("🚀") == 2
    assert displaywidth("é") == 1


def test_pad():
    assert pad("ab", 4) == "ab  "
    assert pad("ab", 4, align="right") == "  ab"
    assert pad("ab", 5, align="center") == " ab  "
    assert pad("abcdef", 4) == "abcdef"
    assert pad("你好", 6) == "你好  "


def test_line_plain_and_str_conversion():
    assert str(Line()("n=", 42)) == "n=42"


def test_line_color_auto_resets_on_next_call():
    assert str(Line().user("Alice")(" by ")) == f"{ESC}34mAlice{ESC}0m by "


def test_line_str_restores_active_color():
    assert str(Line().user("Alice")) == f"{ESC}34mAlice{ESC}0m"


def test_line_same_color_not_reemitted():
    assert str(Line().user("a").user("b")) == f"{ESC}34mab{ESC}0m"


def test_line_transition_folds_reset_into_one_sequence():
    # bold blue -> plain blue: the bold clear rides in the same sequence
    assert str(Line().action("a").user("b")) == f"{ESC}1;34ma{ESC}0;34mb{ESC}0m"


def test_line_unknown_color_raises():
    with pytest.raises(AttributeError, match="unknown color"):
        Line().nosuchcolor("x")


def test_line_palette_addition(monkeypatch):
    monkeypatch.setattr(colors, "session", "38;5;226", raising=False)
    assert str(Line().session("3")) == f"{ESC}38;5;226m3{ESC}0m"


def test_line_palette_override_takes_effect(monkeypatch):
    monkeypatch.setattr(colors, "user", "36")
    assert str(Line().user("x")) == f"{ESC}36mx{ESC}0m"


def test_line_custom_palette():
    palette = Colors()
    palette.brand = "35"
    assert str(Line(palette).brand("x")) == f"{ESC}35mx{ESC}0m"


def test_line_width_and_align():
    assert str(Line()("ab", width=4)) == "ab  "
    assert str(Line()("ab", width=4, align="right")) == "  ab"
    assert str(Line().user("ab", width=4)) == f"{ESC}34mab  {ESC}0m"
