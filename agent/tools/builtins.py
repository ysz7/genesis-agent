"""The irreducible set of built-in tools, registered on every agent.

``read_file`` · ``write_file`` · ``list_dir`` · ``run_shell`` (the workhorse) ·
``fetch_url`` · ``web_search``. Each is a plain function with a docstring + type
hints; Pydantic
AI derives the JSON schema from the signature, so there is no schema code of our
own. Tools that need shared state take ``RunContext[AgentDeps]`` as the first
parameter and reach the http client / store / settings via ``ctx.deps``.

**Filesystem sandbox.** ``read_file`` / ``write_file`` / ``list_dir`` resolve
their argument and refuse anything that lands outside the agent's ``workspace/``
— relative ``../`` escapes and absolute paths to elsewhere both return an error
string to the model rather than touching the host filesystem. Set
``sandbox: false`` in ``settings.yaml`` to opt out (trusted setups only). This
guard does NOT extend to ``run_shell``: a shell command can ``cd`` anywhere, so
treat ``run_shell`` as full host access and gate it via the tool policy
(``tools.confirm`` / ``tools.disable``) when that matters.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic_ai import RunContext

from ..runtime.context import AgentDeps
from .toolkit import html_to_text, web_search as _web_search

#: Fallback cap on a single tool's output (characters) when settings don't set
#: ``max_tool_output``. ~20k chars ≈ 5k tokens.
DEFAULT_MAX_TOOL_OUTPUT = 20000


def _output_cap(ctx: RunContext[AgentDeps]) -> int:
    return int(ctx.deps.settings.get("max_tool_output", DEFAULT_MAX_TOOL_OUTPUT))


def _window_note(unit: str, start: int, end: int, total: int) -> str:
    """A one-line navigation footer telling the model the window moved.

    Shared by ``read_file`` (lines) and ``fetch_url`` (chars) so a paginated
    tool result always reads the same way: the data was not lost, the window
    advanced, and ``offset=<end>`` fetches the next page. ``start``/``end`` are
    the human 1-indexed bounds of *this* page; ``end`` doubles as the 0-indexed
    offset for the next call (line/char N+1 begins at offset N).
    """
    return (
        f"\n\n…(showing {unit} {start}-{end} of {total}; "
        f"call again with offset={end})"
    )


class _SandboxEscape(Exception):
    """A path resolved outside the workspace while the sandbox was enabled."""


def _resolve(ctx: RunContext[AgentDeps], path: str) -> Path:
    """Resolve *path* for a file tool, enforcing the sandbox.

    Relative paths resolve inside ``workspace/files/`` (the agent's default
    working area — task outputs stay separate from self-authored code under
    ``workspace/tools`` etc.); reach the siblings with ``../tools/x.py``.
    Absolute paths are taken as given. With the sandbox on (the default), the
    resolved target must stay within the resolved ``workspace/`` (not just
    ``files/``) or :class:`_SandboxEscape` is raised. Both sides are
    ``.resolve()``-d so symlinks and Windows drive-letter casing compare
    like-for-like. ``sandbox: false`` restores raw resolution.
    """
    workspace = ctx.deps.workspace
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = ctx.deps.files_dir / candidate

    if ctx.deps.settings.get("sandbox", True) is False:
        return candidate

    resolved = candidate.resolve()
    if not resolved.is_relative_to(workspace.resolve()):
        raise _SandboxEscape(
            f"Error: path escapes the workspace sandbox: {path}"
        )
    return resolved


def read_file(
    ctx: RunContext[AgentDeps],
    path: str,
    offset: int = 0,
    limit: int | None = None,
) -> str:
    """Read and return the text contents of a file.

    Large files are paginated by line so nothing is silently lost: a window of
    lines is returned and, if more remain, a footer tells you how many lines the
    file has and how to fetch the next page. Even without ``limit`` the output
    is capped at ``max_tool_output`` characters (the per-call window) — page
    through the rest with ``offset`` rather than trying to read it all at once.

    Args:
        path: File path. Relative paths are resolved inside the workspace;
            paths outside the workspace are refused unless the sandbox is off.
        offset: 0-indexed line to start from (default 0 = the file's start).
        limit: Maximum number of lines to return in this call. ``None`` (the
            default) returns as many as fit in the character window.
    """
    try:
        target = _resolve(ctx, path)
    except _SandboxEscape as exc:
        return str(exc)
    if not target.exists():
        return f"Error: file not found: {target}"
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"Error: {target} is not a UTF-8 text file."

    lines = text.splitlines(keepends=True)
    total = len(lines)
    start = max(0, offset)
    if total and start >= total:
        return f"(offset {offset} is at or beyond end of file; {total} lines total)"

    cap = _output_cap(ctx)
    hard_end = total if limit is None else min(start + max(0, limit), total)
    end, size = start, 0
    while end < hard_end:
        ln = lines[end]
        if size and size + len(ln) > cap:  # keep at least one line, then honour the cap
            break
        size += len(ln)
        end += 1

    window = "".join(lines[start:end])
    if end < total:
        window += _window_note("lines", start + 1, end, total)
    return window


def write_file(ctx: RunContext[AgentDeps], path: str, content: str) -> str:
    """Write text to a file, creating parent directories as needed.

    Args:
        path: Destination path. Relative paths are written inside the workspace;
            paths outside the workspace are refused unless the sandbox is off.
        content: The full text to write (overwrites any existing file).
    """
    try:
        target = _resolve(ctx, path)
    except _SandboxEscape as exc:
        return str(exc)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {target}"


def list_dir(ctx: RunContext[AgentDeps], path: str = ".") -> list[str]:
    """List the entries of a directory (directories are suffixed with '/').

    Args:
        path: Directory path. Relative paths are resolved inside the workspace;
            paths outside the workspace are refused unless the sandbox is off.
    """
    try:
        target = _resolve(ctx, path)
    except _SandboxEscape as exc:
        return [str(exc)]
    if not target.exists():
        return [f"Error: directory not found: {target}"]
    if not target.is_dir():
        return [f"Error: not a directory: {target}"]
    return sorted(
        f"{e.name}/" if e.is_dir() else e.name for e in target.iterdir()
    )


def run_shell(ctx: RunContext[AgentDeps], command: str, timeout: int = 120) -> str:
    """Run a shell command in the workspace and return its combined output.

    The workhorse tool: use it for builds, tests, git, file manipulation, and
    anything not covered by a dedicated tool.

    Args:
        command: The shell command line to execute.
        timeout: Seconds before the command is killed (default 120).
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(ctx.deps.workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s"
    out = (proc.stdout or "") + (proc.stderr or "")
    out = out.strip() or "(no output)"
    if proc.returncode != 0:
        out = f"[exit {proc.returncode}]\n{out}"
    cap = _output_cap(ctx)
    if len(out) > cap:
        # Shell output isn't stable across re-runs, so an offset is meaningless;
        # show the head and tell the model to narrow the command instead.
        return out[:cap] + (
            f"\n\n[output capped: showing first {cap} of {len(out)} chars (head); "
            f"re-run narrowing the command, e.g. piping through grep/head/tail]"
        )
    return out


def fetch_url(
    ctx: RunContext[AgentDeps],
    url: str,
    raw: bool = False,
    offset: int = 0,
) -> str:
    """Fetch a URL and return its body as readable text.

    HTML pages are stripped to plain text (tags removed, links rendered as
    ``text (href)``) so the model gets prose, not markup; JSON and plain text
    pass through unchanged. Long bodies are paginated by character: a window of
    ``max_tool_output`` chars is returned and, if more remain, a footer says how
    to fetch the next page — no data is silently dropped.

    Args:
        url: The http(s) URL to GET.
        raw: Set True to get the untouched response body (skip HTML cleaning).
        offset: 0-indexed character to start the window from (default 0).
    """
    try:
        resp = ctx.deps.http.get(url)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - surface any transport error to the model
        return f"Error fetching {url}: {exc}"
    text = resp.text
    if not raw and _looks_like_html(resp, text):
        text = html_to_text(text)
    total = len(text)
    start = max(0, offset)
    if total and start >= total:
        return f"(offset {offset} is beyond end of body; {total} chars total)"
    cap = _output_cap(ctx)
    end = min(start + cap, total)
    window = text[start:end]
    if end < total:
        window += _window_note("chars", start + 1, end, total)
    return window


def _looks_like_html(resp, text: str) -> bool:
    """True when the response is HTML by content-type or by a leading tag."""
    if "html" in resp.headers.get("content-type", "").lower():
        return True
    head = text.lstrip()[:200].lower()
    return head.startswith(("<!doctype html", "<html")) or "<html" in head


def web_search(ctx: RunContext[AgentDeps], query: str, max_results: int = 5) -> str:
    """Search the web for current information and return the top results.

    Use this for anything you don't already know or that changes over time
    (news, prices, weather, docs, "today"…): search, then ``fetch_url`` a
    promising result link to read it in full. Returns a numbered list of
    title · URL · snippet. Powered by DuckDuckGo (no API key).

    Args:
        query: What to search for.
        max_results: How many results to return (default 5).
    """
    results = _web_search(query, client=ctx.deps.http, max_results=max_results)
    if not results:
        return (
            f"No results for {query!r} (the search endpoint may be rate-limited). "
            f"If you know a likely URL, try fetch_url instead."
        )
    blocks = [
        f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}".rstrip()
        for i, r in enumerate(results, 1)
    ]
    return "\n".join(blocks)[: _output_cap(ctx)]


#: The built-in tool functions, in registration order.
BUILTIN_TOOLS = [read_file, write_file, list_dir, run_shell, fetch_url, web_search]
