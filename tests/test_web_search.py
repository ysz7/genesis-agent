"""Web search tool (DuckDuckGo HTML) — parser, helper, and the builtin wrapper.

No network: a ``httpx.MockTransport`` returns a canned DuckDuckGo results page.
"""

from types import SimpleNamespace

import httpx

from agent.tools import builtins
from agent.tools.toolkit import _ddg_real_url, _parse_ddg, web_search

_SAMPLE = """
<div class="result">
  <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fmilan&rut=x">
    Milan <b>weather</b> today
  </a>
  <a class="result__snippet">Currently 24&deg;C and clear in Milan.</a>
</div>
<div class="result">
  <a class="result__a" href="https://direct.example.org/page">Second result</a>
  <a class="result__snippet">Another <b>snippet</b> here.</a>
</div>
"""


def test_ddg_real_url_decodes_redirect():
    assert _ddg_real_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.com%2Fx&rut=z") == "https://a.com/x"
    assert _ddg_real_url("https://direct.example.org/page") == "https://direct.example.org/page"


def test_parse_ddg_extracts_results():
    results = _parse_ddg(_SAMPLE, max_results=5)
    assert len(results) == 2
    assert results[0]["title"] == "Milan weather today"
    assert results[0]["url"] == "https://example.com/milan"
    assert "24" in results[0]["snippet"] and "<" not in results[0]["snippet"]
    assert results[1]["url"] == "https://direct.example.org/page"


def test_parse_ddg_respects_max():
    assert len(_parse_ddg(_SAMPLE, max_results=1)) == 1


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_web_search_helper_posts_query():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(200, content=_SAMPLE.encode())

    results = web_search("weather milan", client=_client(handler), max_results=5)
    assert "html.duckduckgo.com" in seen["url"]
    assert "weather+milan" in seen["body"] or "weather%20milan" in seen["body"]
    assert results[0]["url"] == "https://example.com/milan"


def test_web_search_empty_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)
    assert web_search("x", client=_client(handler)) == []


# ── builtin wrapper ───────────────────────────────────────────────────────────

def _ctx(handler):
    deps = SimpleNamespace(http=_client(handler), settings={})
    return SimpleNamespace(deps=deps)


def test_builtin_web_search_formats_results():
    out = builtins.web_search(_ctx(lambda r: httpx.Response(200, content=_SAMPLE.encode())), "milan")
    assert "1. Milan weather today" in out
    assert "https://example.com/milan" in out
    assert "2. Second result" in out


def test_builtin_web_search_no_results_message():
    out = builtins.web_search(_ctx(lambda r: httpx.Response(200, content=b"<html></html>")), "zzz")
    assert "No results" in out


def test_web_search_is_a_builtin():
    assert builtins.web_search in builtins.BUILTIN_TOOLS
