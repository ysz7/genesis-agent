"""Phase 28: full run transcripts — opt-in, one JSONL file per run.

``runs.jsonl`` (``runlog.py``) keeps aggregates only (task, duration, tokens,
ok); debugging "why did the agent do X yesterday" needs the full trace.
Opt-in ``log_transcripts: true`` writes one
``workspace/transcripts/<ts>-<id>.jsonl`` per run: a header line (task,
duration, tokens, ok/error) followed by one line per message part (role, tool
name, args, truncated content) — walked from the Pydantic AI result's
``all_messages()`` the same way regardless of caller, so the CLI, server,
scheduler, and gateways all produce identical records. Args/content are
redacted the same as any other tool output (``runtime/secrets.py``) before
they touch disk. Bounded by ``transcripts_keep`` (default 200 files, oldest
pruned). Failures to write are logged and swallowed — a transcript must never
break a run.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .secrets import redact_value

logger = logging.getLogger("agent.transcripts")

_TRUNC = 4000              # per-part content cap — this file IS the detail
DEFAULT_KEEP = 200


def _trunc(value: Any) -> str:
    s = value if isinstance(value, str) else str(value)
    return s if len(s) <= _TRUNC else s[: _TRUNC - 1] + "…"


def _role_for(msg_kind: str, part_kind: str) -> str:
    """Map a Pydantic AI (message kind, part kind) pair to a log-friendly role."""
    if part_kind == "system-prompt":
        return "system"
    if part_kind == "user-prompt":
        return "user"
    if part_kind in ("tool-return", "retry-prompt"):
        return "tool"
    return "assistant" if msg_kind == "response" else "user"


def _part_line(msg_kind: str, part: Any, secret_values: dict) -> dict:
    kind = getattr(part, "part_kind", "?")
    line: dict = {"role": _role_for(msg_kind, kind), "part": kind}
    tool_name = getattr(part, "tool_name", None)
    if tool_name:
        line["tool_name"] = tool_name
    args = getattr(part, "args", None)
    if args is not None:
        line["args"] = redact_value(args, secret_values) if secret_values else args
    content = getattr(part, "content", None)
    if content is not None:
        content = redact_value(content, secret_values) if secret_values else content
        line["content"] = _trunc(content)
    outcome = getattr(part, "outcome", None)
    if outcome and outcome != "success":
        line["outcome"] = outcome
    return line


def _messages_to_lines(messages: list, secret_values: dict) -> list[dict]:
    lines = []
    for msg in messages or []:
        kind = getattr(msg, "kind", "?")
        for part in getattr(msg, "parts", []) or []:
            lines.append(_part_line(kind, part, secret_values))
    return lines


def _usage_tokens(result: Any) -> int | None:
    try:
        u = result.usage
        usage = u if hasattr(u, "input_tokens") else u()
        return (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0)
    except Exception:  # noqa: BLE001 - best-effort only
        return None


def _prune(transcripts_dir: Path, keep: int) -> None:
    """Delete the oldest transcripts beyond *keep*, oldest-mtime-first.

    Sorted by mtime (not filename) — two runs finishing within the same
    second get names that differ only by a random suffix, so filename order
    wouldn't reliably reflect write order.
    """
    files = sorted(transcripts_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime)
    excess = len(files) - keep
    for f in files[: max(0, excess)]:
        try:
            f.unlink()
        except OSError:
            pass


def write_transcript(
    deps,
    task: str,
    *,
    result: Any = None,
    duration: float,
    ok: bool,
    error: str | None = None,
) -> None:
    """Write one transcript file for this run, if ``log_transcripts`` is enabled."""
    if not deps.settings.get("log_transcripts"):
        return
    header: dict = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "task": str(task)[:500],
        "duration_s": round(duration, 2),
        "ok": ok,
    }
    tokens = _usage_tokens(result) if result is not None else None
    if tokens is not None:
        header["tokens"] = tokens
    if error:
        header["error"] = str(error)[:500]

    messages: list = []
    if result is not None:
        try:
            messages = result.all_messages()
        except Exception:  # noqa: BLE001 - a transcript must never break a run
            messages = []

    try:
        transcripts_dir = deps.workspace / "transcripts"
        transcripts_dir.mkdir(parents=True, exist_ok=True)
        name = f"{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}.jsonl"
        lines = [header] + _messages_to_lines(messages, deps.secrets)
        with (transcripts_dir / name).open("w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        keep = int(deps.settings.get("transcripts_keep", DEFAULT_KEEP) or DEFAULT_KEEP)
        _prune(transcripts_dir, keep)
    except Exception as exc:  # noqa: BLE001 - the transcript must never break a run
        logger.warning("could not write transcript: %s", exc)
