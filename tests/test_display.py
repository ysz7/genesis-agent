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


# ── prompt_toolkit line reader ───────────────────────────────────────────────

def test_read_line_fallback_uses_input(monkeypatch):
    # With no session (None), read_line mirrors input().
    monkeypatch.setattr("builtins.input", lambda *a, **k: "typed line")
    assert display.read_line(None, "> ") == "typed line"


def test_new_prompt_session_none_when_not_tty(monkeypatch):
    # Non-interactive (pytest, pipes) → None, so the REPL falls back to input().
    monkeypatch.setattr("sys.stdin", type("S", (), {"isatty": staticmethod(lambda: False)})())
    assert display.new_prompt_session(".") is None


def test_safe_text_recombines_surrogates():
    # Windows emoji input arrives as lone surrogate halves → must become valid.
    broken = "hi " + "\ud83c" + "\udfaf" + " ok"     # "🎯" split in two
    fixed = display._safe_text(broken)
    assert fixed == "hi 🎯 ok"
    fixed.encode("utf-8")                              # must not raise
    # plain text is unchanged
    assert display._safe_text("plain") == "plain"
    # CRLF / CR (stray ^M from Windows pastes) normalized to LF
    assert display._safe_text("a\r\nb\rc") == "a\nb\nc"


def test_placeholder_span_atomic_delete():
    ph = "[Pasted text #1 +9 lines]"
    text = f"hi {ph} there"
    store = {ph: "the real pasted text"}
    start = text.index(ph)
    end = start + len(ph)
    # Backspace (before): cursor just after / inside the placeholder → whole span
    assert display._placeholder_span(text, end, store, before=True) == (start, end, ph)
    assert display._placeholder_span(text, start + 3, store, before=True) == (start, end, ph)
    # outside the placeholder → None (normal single-char delete)
    assert display._placeholder_span(text, 2, store, before=True) is None
    # Delete (forward): cursor at start / inside
    assert display._placeholder_span(text, start, store, before=False) == (start, end, ph)
    assert display._placeholder_span(text, end, store, before=False) is None
