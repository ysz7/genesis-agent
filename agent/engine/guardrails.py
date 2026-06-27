"""Phase 21: opt-in input/output guardrails — a lean content-safety seam.

Beyond the tool-policy / sandbox (which guard *tool calls*), this adds a thin
layer on a run's text **in** and **out**, configured in ``settings.yaml``::

    guardrails:
      input:
        block:  ["(?i)\\bssn\\b"]      # matching input is refused (never sent)
        redact: ["\\b\\d{16}\\b"]       # matching spans → [redacted] before sending
      output:
        block:  ["(?i)api[_-]?key"]     # matching output → one retry, then clean fail
        redact: ["\\b\\d{16}\\b"]       # matching spans → [redacted] in the answer

Regex-based and **off by default** (zero overhead). genesis ships the *seam*, not
a policy library — there is deliberately NO NeMo / Guardrails-AI dependency.
Prompt-injection defence stays primarily the tool-policy story; this is the
content layer on top. A vertical needing custom logic adds its own
``@agent.output_validator`` in its copy of ``factory.py``.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("agent.guardrails")

REDACTED = "[redacted]"


def enabled(settings: dict) -> bool:
    """True when a ``guardrails:`` block is configured."""
    return bool(settings.get("guardrails"))


def _compile(patterns) -> list[re.Pattern]:
    """Compile a list of regex patterns, skipping (with a warning) any bad one."""
    out: list[re.Pattern] = []
    for p in patterns or []:
        try:
            out.append(re.compile(str(p)))
        except re.error as exc:
            logger.warning("ignoring invalid guardrail pattern %r: %s", p, exc)
    return out


def _section(settings: dict, which: str) -> dict:
    g = settings.get("guardrails") or {}
    sec = g.get(which) or {}
    return sec if isinstance(sec, dict) else {}


def check_input(settings: dict, text: str) -> tuple[bool, str]:
    """Apply input guardrails to *text*.

    Returns ``(allowed, value)``: ``(False, message)`` when a ``block`` pattern
    matches (the run should be refused, not executed); otherwise ``(True, text)``
    with any ``redact`` patterns replaced by ``[redacted]``. No guardrails → the
    text passes through unchanged.
    """
    inp = _section(settings, "input")
    if not inp:
        return True, text
    for rx in _compile(inp.get("block")):
        if rx.search(text):
            return False, "Refused: input blocked by a guardrail policy; not executed."
    for rx in _compile(inp.get("redact")):
        text = rx.sub(REDACTED, text)
    return True, text


def output_validator_for(settings: dict):
    """Return an output-validator callable for ``factory``, or None if unconfigured.

    A ``block`` match raises ``ModelRetry`` (Pydantic AI re-asks the model, up to
    the agent's ``retries``; then the run fails cleanly). ``redact`` patterns are
    replaced in a string output. Non-string (structured) output is checked for
    ``block`` against its string form but only redacted when it's a string.
    """
    out = _section(settings, "output")
    block = _compile(out.get("block"))
    redact = _compile(out.get("redact"))
    if not block and not redact:
        return None

    from pydantic_ai import ModelRetry

    def validate_output(value):
        text = value if isinstance(value, str) else str(value)
        for rx in block:
            if rx.search(text):
                raise ModelRetry(
                    "Your answer violated a content policy; rewrite it without the "
                    "disallowed content."
                )
        if isinstance(value, str):
            for rx in redact:
                value = rx.sub(REDACTED, value)
        return value

    return validate_output
