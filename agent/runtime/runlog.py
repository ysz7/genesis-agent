"""Optional local run log — one JSON line per run, zero external services.

Enabled by ``log_runs: true`` in ``settings.yaml``; every run (CLI, server,
scheduler) then appends a line to ``workspace/runs.jsonl``::

    {"ts": "...", "task": "...", "duration_s": 3.1, "tokens": 684, "ok": true}

Greppable history through the existing workspace seam. Failures to write are
logged and swallowed — the run log must never break a run.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("agent.runlog")


def append_run(
    deps,
    task: str,
    duration: float,
    tokens: int,
    ok: bool,
    error: str | None = None,
) -> None:
    """Append one JSON line for a finished run, if ``log_runs`` is enabled."""
    if not deps.settings.get("log_runs"):
        return
    line: dict = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "task": str(task)[:500],
        "duration_s": round(duration, 2),
        "tokens": tokens,
        "ok": ok,
    }
    if error:
        line["error"] = str(error)[:500]
    try:
        path = deps.workspace / "runs.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 - the run log must never break a run
        logger.warning("could not write runs.jsonl: %s", exc)
