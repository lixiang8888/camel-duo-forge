"""目的：整个项目唯一的输入，格式上的错要在这里全部挡住。"""

from __future__ import annotations

import pytest

from duo_forge.purpose import (
    MAX_PURPOSE,
    MAX_TITLE,
    InvalidPurpose,
    make_purpose,
    read_purpose_file,
)


def test_strips_whitespace():
    made = make_purpose("  给程序员写一份颈椎保养手册  ", "  颈椎手册  ")
    assert made.text == "给程序员写一份颈椎保养手册"
    assert made.title == "颈椎手册"


def test_title_is_optional():
    assert make_purpose("给程序员写一份颈椎保养手册").title == ""


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_empty_purpose_is_refused(text):
    with pytest.raises(InvalidPurpose) as excinfo:
        make_purpose(text)
    assert "没有写目的" in str(excinfo.value)


def test_too_short_purpose_is_refused():
    """「帮我写点东西」这种推不出任何人格来，不如当场说清楚。"""
    with pytest.raises(InvalidPurpose) as excinfo:
        make_purpose("写点东西")
    assert "太短" in str(excinfo.value)


def test_too_long_purpose_is_refused():
    with pytest.raises(InvalidPurpose) as excinfo:
        make_purpose("长" * (MAX_PURPOSE + 1))
    assert "太长" in str(excinfo.value)


def test_too_long_title_is_refused():
    with pytest.raises(InvalidPurpose) as excinfo:
        make_purpose("给程序员写一份颈椎保养手册", "标" * (MAX_TITLE + 1))
    assert "标题太长" in str(excinfo.value)


def test_non_text_purpose_is_refused():
    with pytest.raises(InvalidPurpose):
        make_purpose(None)  # type: ignore[arg-type]


def test_summary_shortens_a_long_first_line():
    made = make_purpose("首" * 100, "标题")
    assert made.summary().startswith("标题 · 首")
    assert len(made.summary()) < 60


def test_summary_says_so_when_the_title_is_pending():
    assert "标题待生成" in make_purpose("给程序员写一份颈椎保养手册").summary()


# ---------------------------------------------------------------------------
# 从文件读
# ---------------------------------------------------------------------------


def test_read_purpose_file(tmp_path):
    path = tmp_path / "p.txt"
    path.write_text("给程序员写一份颈椎保养手册，要能真的照做。", encoding="utf-8")
    made = read_purpose_file(path)
    assert made.title == ""
    assert made.text.startswith("给程序员")


def test_first_heading_line_becomes_the_title(tmp_path):
    path = tmp_path / "p.md"
    path.write_text(
        "# 程序员颈椎保养手册\n\n给刚上班、每天坐十小时的程序员写一份手册。",
        encoding="utf-8",
    )
    made = read_purpose_file(path)
    assert made.title == "程序员颈椎保养手册"
    assert made.text.startswith("给刚上班")
    assert "#" not in made.text


def test_missing_purpose_file():
    with pytest.raises(InvalidPurpose) as excinfo:
        read_purpose_file("/nonexistent/purpose.txt")
    assert "读不出" in str(excinfo.value)


def test_empty_purpose_file_is_refused(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("   \n", encoding="utf-8")
    with pytest.raises(InvalidPurpose):
        read_purpose_file(path)
