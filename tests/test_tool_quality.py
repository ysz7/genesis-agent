"""Phase 7 tool quality: HTML→text, output cap, registry dup/annotation checks."""

import logging
from types import SimpleNamespace

import httpx

from agent.tools.toolkit import html_to_text
from agent.tools.builtins import fetch_url, run_shell
from agent.runtime.config import load_config
from agent.runtime.context import build_deps, close_deps
from agent.engine.registry import discover_tools, tool_names


# ── HTML → text ──────────────────────────────────────────────────────────────

def test_html_to_text_strips_and_renders_links():
    html = """
    <html><head><title>T</title><style>.x{color:red}</style></head>
    <body>
      <script>var evil = 1;</script>
      <h1>Hello</h1>
      <p>Some <b>bold</b> text and a <a href="https://ex.com">link</a>.</p>
    </body></html>
    """
    out = html_to_text(html)
    assert "<" not in out and ">" not in out      # no tags
    assert "var evil" not in out and "color:red" not in out  # script/style dropped
    assert "Hello" in out and "Some bold text" in out
    assert "link (https://ex.com)" in out          # link rendered inline


# ── fetch_url ────────────────────────────────────────────────────────────────

def _ctx_with_responses(tmp_path, handler, **settings):
    cfg = load_config(tmp_path)
    deps = build_deps(cfg)
    deps.settings.update(settings)
    deps.http.close()
    deps.http = httpx.Client(transport=httpx.MockTransport(handler))
    return SimpleNamespace(deps=deps), deps


def test_fetch_url_cleans_html(tmp_path):
    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/html"},
            text="<html><body><p>Breaking <b>news</b> today</p>"
                 "<script>x()</script></body></html>",
        )
    ctx, deps = _ctx_with_responses(tmp_path, handler)
    try:
        out = fetch_url(ctx, "http://x/")
        assert "<" not in out
        assert "Breaking news today" in out
        assert "x()" not in out
        # raw=True returns the untouched markup.
        assert "<script>" in fetch_url(ctx, "http://x/", raw=True)
    finally:
        close_deps(deps)


def test_fetch_url_json_passthrough(tmp_path):
    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "application/json"}, text='{"a": 1}'
        )
    ctx, deps = _ctx_with_responses(tmp_path, handler)
    try:
        assert fetch_url(ctx, "http://x/") == '{"a": 1}'
    finally:
        close_deps(deps)


def test_max_tool_output_windows_fetch(tmp_path):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="a" * 500)
    ctx, deps = _ctx_with_responses(tmp_path, handler, max_tool_output=50)
    try:
        out = fetch_url(ctx, "http://x/")
        assert out.startswith("a" * 50)                # the window body
        assert "showing chars 1-50 of 500" in out      # navigation footer, not silent loss
        assert "offset=50" in out
        # Paging with offset returns the disjoint next window.
        nxt = fetch_url(ctx, "http://x/", offset=50)
        assert nxt.startswith("a" * 50) and "showing chars 51-100 of 500" in nxt
        # An offset past the end is reported, not an empty string.
        assert "beyond end" in fetch_url(ctx, "http://x/", offset=500)
    finally:
        close_deps(deps)


def test_max_tool_output_caps_run_shell(tmp_path):
    cfg = load_config(tmp_path)
    deps = build_deps(cfg)
    deps.settings["max_tool_output"] = 10
    ctx = SimpleNamespace(deps=deps)
    try:
        out = run_shell(ctx, "echo " + "a" * 100)
        assert out.startswith("a" * 10)               # head shown up to the cap
        assert "output capped" in out                 # told it was capped, with how to narrow
    finally:
        close_deps(deps)


# ── Registry checks ──────────────────────────────────────────────────────────

def test_duplicate_tool_name_skipped(tmp_path, caplog):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    # A vertical tool that collides with the builtin `read_file`.
    (tools_dir / "dup.py").write_text(
        'def read_file(path: str) -> str:\n    """Shadow attempt."""\n    return path\n',
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="agent.registry"):
        names = tool_names(discover_tools(load_config(tmp_path)))
    assert names.count("read_file") == 1            # only the builtin survives
    assert any("duplicate tool name" in r.message for r in caplog.records)


def test_unannotated_param_skipped(tmp_path, caplog):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "mix.py").write_text(
        'def good(x: int) -> int:\n    """Doubles."""\n    return x * 2\n\n\n'
        'def bad(x) -> int:\n    """Missing annotation on x."""\n    return x\n',
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="agent.registry"):
        names = tool_names(discover_tools(load_config(tmp_path)))
    assert "good" in names
    assert "bad" not in names
    assert any("no type annotation" in r.message for r in caplog.records)
