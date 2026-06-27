"""Final-answer rendering: Markdown by default, raw when disabled."""

from rich.console import Console

from agent.console import display


def _render(text, **kw) -> str:
    orig = display.console
    display.console = Console(record=True, width=80)
    try:
        display.answer(text, **kw)
        return display.console.export_text()
    finally:
        display.console = orig


def test_answer_renders_markdown():
    out = _render("**bold** text\n- item one\n- item two\n\n`code`")
    assert "**" not in out and "`" not in out          # syntax rendered, not shown
    assert "bold" in out and "item one" in out and "code" in out


def test_answer_plain_keeps_raw_when_disabled():
    out = _render("**bold**", markdown=False)
    assert "**bold**" in out                            # verbatim


def test_answer_never_crashes_on_odd_input():
    # Whatever the content, rendering must not raise (falls back to plain text).
    assert "x" in _render("x")
    _render("")                                          # empty is fine too
