"""A golden-task eval scored by an LLM judge — copy and adapt this file.

Run it (after installing the extra) from the agent's own folder::

    uv sync --extra evals
    uv run python evals/example_judge.py

Like ``example_eval.py`` this runs the real configured Agent on golden tasks,
but instead of a substring check it grades each answer with an **LLM judge** —
a second model call that scores the output against a rubric. Use a judge when
"correct" is fuzzy: paraphrase, tone, format, or reasoning where no fixed
substring captures a good answer. Keep the plain ``Contains`` style for exact,
cheap checks; reach for a judge only where it earns its cost.

The judge runs on your CONFIGURED provider/model (same ``.env`` as the agent),
so no extra key or setup. Judging is cheaper than generating — to cut cost,
point it at a smaller model of the same provider (see ``JUDGE_MODEL`` below).

The core agent never imports ``pydantic_evals`` — evals stay opt-in and
lean-by-default, exactly like the MCP and obs extras.
"""

from __future__ import annotations

import sys
from pathlib import Path

# This script lives in evals/ next to the agent package; make sure that package
# is importable when run as `python evals/example_judge.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.runtime.config import load_config
from agent.runtime.context import build_deps, close_deps
from agent.engine.factory import build_agent
from agent.engine.model import build_model

try:
    from pydantic_evals import Case, Dataset
    from pydantic_evals.evaluators import LLMJudge
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


# ── The judge: a model that grades answers against a rubric ───────────────────

# Grade on the SAME provider/key as the agent. To spend less, judge with a
# smaller model of your provider — uncomment one before build_model, e.g.:
#   _cfg.model = "gpt-4o-mini"        # openai
#   _cfg.model = "claude-haiku-4-5"   # anthropic
_cfg = load_config(AGENT_ROOT)
JUDGE_MODEL = build_model(_cfg)

# One rubric, applied to every case. The judge sees the task (include_input) and
# the case's expected_output (include_expected_output) as the reference for a
# good answer — so a single rubric generalizes across varied cases without a
# per-case prompt. Keep it strict: reward correct, direct answers; fail evasions.
JUDGE_RUBRIC = (
    "You are grading an AI assistant's answer to the task shown. Treat the "
    "expected output as a reference for a correct answer — the assistant need "
    "not match it word-for-word, but its answer must be consistent with it and "
    "convey the same key information or intent. PASS only if the answer is "
    "correct, directly addresses the task, and follows any format or tone the "
    "task asks for. FAIL if it is wrong, off-topic, evasive, refuses a "
    "reasonable task, or buries the answer in irrelevant text."
)


# ── The golden dataset — cases where a substring check falls short ────────────

dataset = Dataset(
    name="example_judge",
    cases=[
        Case(
            name="paraphrase",
            inputs="In one sentence, explain what version control is.",
            expected_output=(
                "A system that records changes to files over time so you can "
                "review history and revert to earlier versions."
            ),
        ),
        Case(
            name="summarize",
            inputs=(
                "Summarize in one sentence: Photosynthesis is the process by "
                "which green plants use sunlight to turn water and carbon "
                "dioxide into glucose and oxygen."
            ),
            expected_output=(
                "Plants use sunlight to convert water and carbon dioxide into "
                "glucose and oxygen."
            ),
        ),
        Case(
            name="tone",
            inputs=(
                "A user writes: 'Thanks, that fixed my bug!' Reply in one short, "
                "warm sentence."
            ),
            expected_output="A brief, friendly, welcoming acknowledgement.",
        ),
    ],
    evaluators=[
        LLMJudge(
            rubric=JUDGE_RUBRIC,
            model=JUDGE_MODEL,
            include_input=True,
            include_expected_output=True,
        )
    ],
)


if __name__ == "__main__":
    _force_utf8()
    report = dataset.evaluate_sync(run_agent)
    report.print(include_input=True, include_output=True)
