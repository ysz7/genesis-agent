"""Optional reusable helpers so tools don't reinvent common plumbing.

Nothing in the core depends on this module — it's a convenience shelf for the
tools you write: HTTP with retries, a tiny TTL cache, and a dependency-free RSS
parser. Import what you need inside ``tools/*.py``.
"""

from __future__ import annotations

import html as _html
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable

import httpx


def http_get(
    url: str,
    *,
    client: httpx.Client | None = None,
    retries: int = 3,
    backoff: float = 0.5,
    **kwargs: Any,
) -> httpx.Response:
    """GET *url* with exponential backoff on transport/5xx errors."""
    owns = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                resp = client.get(url, **kwargs)
                if resp.status_code < 500:
                    return resp
                last_exc = httpx.HTTPStatusError(
                    f"server error {resp.status_code}", request=resp.request, response=resp
                )
            except httpx.HTTPError as exc:
                last_exc = exc
            time.sleep(backoff * (2**attempt))
        assert last_exc is not None
        raise last_exc
    finally:
        if owns:
            client.close()


class TTLCache:
    """A tiny in-process cache with per-entry time-to-live."""

    def __init__(self, ttl: float = 300.0):
        self.ttl = ttl
        self._data: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any, default: Any = None) -> Any:
        item = self._data.get(key)
        if item is None:
            return default
        expires, value = item
        if time.time() > expires:
            self._data.pop(key, None)
            return default
        return value

    def set(self, key: Any, value: Any) -> None:
        self._data[key] = (time.time() + self.ttl, value)

    def get_or_set(self, key: Any, producer: Callable[[], Any]) -> Any:
        hit = self.get(key, _MISS)
        if hit is _MISS:
            hit = producer()
            self.set(key, hit)
        return hit


_MISS = object()


@dataclass
class FeedItem:
    title: str
    link: str
    summary: str
    published: str


def parse_rss(xml_text: str, limit: int = 50) -> list[FeedItem]:
    """Parse RSS 2.0 or Atom into a list of :class:`FeedItem` — no deps."""
    items: list[FeedItem] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items

    # RSS 2.0: channel/item
    for node in root.iter("item"):
        items.append(
            FeedItem(
                title=_text(node, "title"),
                link=_text(node, "link"),
                summary=_text(node, "description"),
                published=_text(node, "pubDate"),
            )
        )
        if len(items) >= limit:
            return items

    # Atom: entry (tags are namespaced)
    if not items:
        ns = "{http://www.w3.org/2005/Atom}"
        for node in root.iter(f"{ns}entry"):
            link_el = node.find(f"{ns}link")
            items.append(
                FeedItem(
                    title=_text(node, f"{ns}title"),
                    link=(link_el.get("href") if link_el is not None else ""),
                    summary=_text(node, f"{ns}summary") or _text(node, f"{ns}content"),
                    published=_text(node, f"{ns}updated") or _text(node, f"{ns}published"),
                )
            )
            if len(items) >= limit:
                break
    return items


def _text(node: ET.Element, tag: str) -> str:
    el = node.find(tag)
    return (el.text or "").strip() if el is not None and el.text else ""


# ── HTML → readable text ─────────────────────────────────────────────────────

_DROP = {"script", "style", "head", "noscript", "template", "svg"}
_BLOCK = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "section", "article",
    "header", "footer", "nav", "aside", "blockquote", "pre", "hr", "figure",
    "h1", "h2", "h3", "h4", "h5", "h6",
}


class _TextExtractor(HTMLParser):
    """Collect visible text, dropping markup; links render as ``text (href)``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._drop_depth = 0
        self._href: str | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _DROP:
            self._drop_depth += 1
        elif self._drop_depth:
            return
        elif tag == "a":
            self._href = dict(attrs).get("href")
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP:
            self._drop_depth = max(0, self._drop_depth - 1)
        elif self._drop_depth:
            return
        elif tag == "a":
            if self._href:
                self.parts.append(f" ({self._href})")
            self._href = None
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._drop_depth == 0:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Strip HTML to readable plain text: no tags, collapsed whitespace.

    Drops ``script``/``style``/``head`` and similar, renders links as
    ``anchor text (href)``, and collapses runs of spaces and blank lines. Pure
    stdlib — no dependency. Truncation is the caller's job (do it AFTER cleaning).
    """
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - malformed markup shouldn't raise to the tool
        pass
    raw = "".join(parser.parts)
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in raw.splitlines()]
    out: list[str] = []
    blank = False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


# ── Web search (DuckDuckGo HTML, no API key) ──────────────────────────────────

_SEARCH_URL = "https://html.duckduckgo.com/html/"
# DuckDuckGo's HTML endpoint blocks generic clients; present a browser UA.
_SEARCH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_RESULT_RE = re.compile(r'<a\b([^>]*class="result__a"[^>]*)>(.*?)</a>', re.DOTALL | re.I)
_SNIPPET_RE = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL | re.I)
_HREF_RE = re.compile(r'href="([^"]+)"')


def _strip_tags(s: str) -> str:
    return _html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _ddg_real_url(href: str) -> str:
    """Resolve DuckDuckGo's ``/l/?uddg=<encoded>`` redirect to the target URL."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        uddg = urllib.parse.parse_qs(parsed.query).get("uddg")
        if uddg:
            return urllib.parse.unquote(uddg[0])
    return href


def _parse_ddg(html: str, max_results: int) -> list[dict]:
    snippets = _SNIPPET_RE.findall(html)
    out: list[dict] = []
    for i, match in enumerate(_RESULT_RE.finditer(html)):
        if len(out) >= max_results:
            break
        attrs, title = match.group(1), match.group(2)
        href = _HREF_RE.search(attrs)
        out.append({
            "title": _strip_tags(title),
            "url": _ddg_real_url(href.group(1)) if href else "",
            "snippet": _strip_tags(snippets[i]) if i < len(snippets) else "",
        })
    return out


def web_search(
    query: str,
    *,
    client: httpx.Client | None = None,
    max_results: int = 5,
) -> list[dict]:
    """Search the web via DuckDuckGo's HTML endpoint — no API key.

    Returns a list of ``{"title", "url", "snippet"}`` dicts (best-effort; an
    empty list on any error or rate-limit). The endpoint is unofficial and can
    throttle — for heavy use, swap in a search API (Tavily/Brave) in your tool.
    """
    owns = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        resp = client.post(_SEARCH_URL, data={"q": query}, headers={"User-Agent": _SEARCH_UA})
        resp.raise_for_status()
        return _parse_ddg(resp.text, max_results)
    except httpx.HTTPError:
        return []
    finally:
        if owns:
            client.close()
