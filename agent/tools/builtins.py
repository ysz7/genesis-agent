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


def read_file(ctx: RunContext[AgentDeps], path: str) -> str:
    """Read and return the text contents of a file.

    Args:
        path: File path. Relative paths are resolved inside the workspace;
            paths outside the workspace are refused unless the sandbox is off.
    """
    try:
        target = _resolve(ctx, path)
    except _SandboxEscape as exc:
        return str(exc)
    if not target.exists():
        return f"Error: file not found: {target}"
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"Error: {target} is not a UTF-8 text file."


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
    return out[: _output_cap(ctx)]


def fetch_url(ctx: RunContext[AgentDeps], url: str, raw: bool = False) -> str:
    """Fetch a URL and return its body as readable text.

    HTML pages are stripped to plain text (tags removed, links rendered as
    ``text (href)``) so the model gets prose, not markup; JSON and plain text
    pass through unchanged. Output is truncated to the ``max_tool_output`` cap.

    Args:
        url: The http(s) URL to GET.
        raw: Set True to get the untouched response body (skip HTML cleaning).
    """
    try:
        resp = ctx.deps.http.get(url)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - surface any transport error to the model
        return f"Error fetching {url}: {exc}"
    text = resp.text
    if not raw and _looks_like_html(resp, text):
        text = html_to_text(text)
    cap = _output_cap(ctx)
    return text[:cap] + ("…(truncated)" if len(text) > cap else "")


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
