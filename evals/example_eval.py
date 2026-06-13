"""A tiny golden-task eval for your vertical — copy and adapt this file.

Run it (after installing the extra) from the agent's own folder::

    uv sync --extra evals
    uv run python evals/example_eval.py

It runs the real configured Agent (your .env provider/model) on a handful of
golden tasks and scores each output with a plain, LLM-judge-free check —
substring containment here. Swap in your own cases and evaluators; the harness
([pydantic-evals](https://ai.pydantic.dev/evals/)) handles concurrency, timing,
and the scored report.

The core agent never imports ``pydantic_evals`` — evals stay opt-in and
lean-by-default, exactly like the MCP and obs extras.
"""

from __future__ import annotations

import sys
from pathlib import Path

# This script lives in evals/ next to the agent package; make sure that package
# is importable when run as `python evals/example_eval.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.runtime.config import load_config
from agent.runtime.context import build_deps, close_deps
from agent.engine.factory import build_agent

try:
    from pydantic_evals import Case, Dataset
    from pydantic_evals.evaluators import Evaluator, EvaluatorContext
except ImportError:
    sys.exit("pydantic-evals isn't installed — run: uv sync --extra evals")


def _force_utf8() -> None:
    """The scored report uses ✔/✗ glyphs; Windows consoles need UTF-8 stdout."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - best effort
            pass


# ── The task under test: run the agent and return its text answer ────────────

# The agent folder to evaluate. Defaults to this repo's root (the base agent);
# point it at your vertical, e.g. examples/rss_research, by changing this.
AGENT_ROOT = Path(__file__).resolve().parent.parent


async def run_agent(task: str) -> str:
    config = load_config(AGENT_ROOT)
    agent = build_agent(config)
    deps = build_deps(config)
    try:
        async with agent:                       # starts MCP servers, if any
            result = await agent.run(task, deps=deps)
        return str(result.output)
    finally:
        close_deps(deps)


# ── A scorer that needs no second model ──────────────────────────────────────

class Contains(Evaluator):
    """Pass if the case's ``expected_output`` appears in the agent's answer."""

    def evaluate(self, ctx: EvaluatorContext) -> bool:
        return str(ctx.expected_output).lower() in str(ctx.output).lower()


# ── The golden dataset ───────────────────────────────────────────────────────

dataset = Dataset(
    name="example",
    cases=[
        Case(name="echo", inputs="Reply with exactly one word: PONG",
             expected_output="pong"),
        Case(name="arithmetic", inputs="What is 2 + 2? Reply with just the number.",
             expected_output="4"),
        Case(name="geography", inputs="Capital of France? One word.",
             expected_output="paris"),
    ],
    evaluators=[Contains()],
)


if __name__ == "__main__":
    _force_utf8()
    report = dataset.evaluate_sync(run_agent)
    report.print(include_input=True, include_output=True)
