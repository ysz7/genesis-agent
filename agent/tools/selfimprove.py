"""Self-improvement tools (Phase 11) — the agent extends itself, safely.

Opt-in via ``settings.yaml``::

    self_improvement:
      enabled: true

When enabled, these tools register alongside the built-ins:

- ``write_skill`` / ``read_skill`` — named markdown procedures under
  ``workspace/skills/``. Not code, so no approval — just the sandbox path check.
  This is the PRIMARY self-improvement path: prefer a skill over a tool unless
  code execution is strictly required.
- ``remember`` — append a one-line lesson to ``workspace/memory/lessons.jsonl``;
  a digest of recent lessons is injected into the system prompt.
- ``write_tool`` — author a Python tool under ``workspace/tools/``. Gated:
  syntax → banned-import scan → load/contract check (the eval-gate) → human
  approval (Phase 11e). Generated tools run only after approval and are loaded
  fresh on the next reload.

Everything authored lives under ``workspace/`` (the Phase 2a sandbox), each file
carries a provenance header, and code never runs until a human approves it —
that approval, not the validation, is the real security boundary.
"""

from __future__ import annotations

import ast
import json
import logging
from datetime import datetime, timezone

from pydantic_ai import RunContext

from ..runtime.context import AgentDeps
from ..runtime.approvals import content_hash, request_approval

logger = logging.getLogger("agent.selfimprove")

# Cheap first-line guards for generated tools — overridable via
# settings `generated_tools: {banned_imports: [...]}`.
DEFAULT_BANNED_IMPORTS = ["subprocess", "socket", "ctypes", "multiprocessing"]
BANNED_NAMES = {"eval", "exec", "compile", "__import__"}


def _safe_stem(name: str) -> str | None:
    """A filesystem-safe identifier stem, or None if *name* isn't usable."""
    stem = "".join(c for c in name.strip() if c.isalnum() or c == "_")
    if not stem or stem[0].isdigit() or stem != name.strip():
        return None
    return stem


# ── Skills (markdown procedures) ─────────────────────────────────────────────

def write_skill(ctx: RunContext[AgentDeps], name: str, content: str) -> str:
    """Save a reusable procedure (a 'skill') as markdown for later recall.

    A skill is a named, multi-step procedure in prose (e.g. "how to produce the
    weekly report") — not code, so it is saved without approval. Prefer writing
    a skill over a tool unless executable code is strictly required.

    Args:
        name: Short identifier (letters, digits, underscores).
        content: The skill's markdown body; the first line should summarize it.
    """
    stem = _safe_stem(name)
    if stem is None:
        return f"Error: invalid skill name {name!r} (use letters, digits, underscores)."
    path = ctx.deps.skills_dir / f"{stem}.md"
    path.write_text(content, encoding="utf-8")
    return f"Saved skill '{stem}' ({len(content)} chars) to workspace/skills/{stem}.md"


def read_skill(ctx: RunContext[AgentDeps], name: str) -> str:
    """Read back the full text of a previously saved skill.

    Args:
        name: The skill's identifier (as given to write_skill).
    """
    stem = _safe_stem(name)
    if stem is None:
        return f"Error: invalid skill name {name!r}."
    path = ctx.deps.skills_dir / f"{stem}.md"
    if not path.exists():
        return f"Error: no skill named '{stem}'."
    return path.read_text(encoding="utf-8")


# ── Reflection memory ────────────────────────────────────────────────────────

def remember(ctx: RunContext[AgentDeps], lesson: str) -> str:
    """Record a one-line lesson learned, for recall in future sessions.

    Use after finishing a task to note what worked or what to avoid. Lessons are
    appended to workspace/memory/lessons.jsonl; a digest of recent ones is shown
    to you at the start of each run.

    Args:
        lesson: A concise, self-contained lesson (one or two sentences).
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lesson": lesson.strip(),
    }
    # Semantic memory (Phase 19): store an embedding so recall can rank by
    # relevance, not just recency. Best-effort — a failed embed just omits it.
    from ..runtime import memory

    if memory.semantic_enabled(ctx.deps.settings):
        vec = memory.embed(ctx.deps, lesson.strip())
        if vec is not None:
            entry["embedding"] = vec
    path = ctx.deps.memory_dir / "lessons.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if memory.semantic_enabled(ctx.deps.settings):
        memory.trim(path, int(memory._opt(ctx.deps.settings, "max_vectors", memory.DEFAULT_MAX_VECTORS)))
    return "Lesson recorded."


# ── Tool authoring ───────────────────────────────────────────────────────────

def _scan_banned(tree: ast.AST, settings: dict) -> str | None:
    """Return the first banned import/name found in *tree*, or None."""
    gen = settings.get("generated_tools") or {}
    banned = set(gen.get("banned_imports") or DEFAULT_BANNED_IMPORTS)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in banned:
                    return f"import {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in banned:
                return f"from {node.module} import ..."
        elif isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            return node.id
    return None


_PROVENANCE_MARK = "# --- genesis-agent: agent-authored tool ---"


def _provenance(description: str, model: str) -> str:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return (
        f"{_PROVENANCE_MARK}\n"
        f"# created: {now}\n"
        f"# task: {description.strip() or '(unspecified)'}\n"
        f"# model: {model}\n"
        "# Auto-generated; runs only after human approval. Edit at your own risk.\n\n"
    )


def _strip_provenance(text: str) -> str:
    """Return generated source without its provenance header.

    Lets ``read_tool`` hand back just the code, so a revision can go straight to
    ``write_tool`` (which prepends a fresh header) without stacking headers.
    """
    if not text.startswith(_PROVENANCE_MARK):
        return text
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines) and lines[i].strip():   # skip the header comment lines
        i += 1
    if i < len(lines) and not lines[i].strip():  # and the blank separator
        i += 1
    return "".join(lines[i:])


def write_tool(ctx: RunContext[AgentDeps], name: str, code: str, description: str) -> str:
    """Author a new Python tool. It runs only after it passes checks AND a human approves.

    Gates, in order: syntax (ast.parse) → banned-import scan → load + tool
    contract (must define a documented, type-hinted function named `name`) →
    human approval. The file is written to workspace/tools/<name>.py with a
    provenance header; on approval it becomes available after the next reload.

    Args:
        name: Tool/function name (letters, digits, underscores) — the file must
            define a function with exactly this name.
        code: The tool's Python source (one documented, type-hinted function).
        description: One line on what the tool does and why (recorded + shown at approval).
    """
    deps = ctx.deps
    stem = _safe_stem(name)
    if stem is None:
        return f"Error: invalid tool name {name!r} (use letters, digits, underscores)."

    # 1. syntax
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"Rejected: syntax error — {exc}"
    # 2. banned imports / names (cheap first line of defense)
    bad = _scan_banned(tree, deps.settings)
    if bad is not None:
        return f"Rejected: disallowed `{bad}` in generated tool code."

    # 3. write with a provenance header
    path = deps.gen_tools_dir / f"{stem}.py"
    full = _provenance(description, deps.config.model) + code.rstrip() + "\n"
    path.write_text(full, encoding="utf-8")

    # 4. eval-gate: does it load and satisfy the tool contract?
    from ..engine.registry import _load_module_functions

    names = [f.__name__ for f in _load_module_functions(path)]
    if stem not in names:
        path.unlink(missing_ok=True)
        return (
            f"Rejected: '{stem}' failed to load or violates the tool contract — "
            f"it must define a function named '{stem}' with a docstring and a "
            f"type annotation on every parameter. File removed; fix and resubmit."
        )

    # 5. human approval for activation (Phase 11e)
    approved = request_approval(
        deps, f"tool:{stem}", content_hash(full),
        detail=f"activate generated tool '{stem}': {description.strip()}",
    )
    if not approved:
        return (
            f"Tool '{stem}' written and validated, but activation was declined — "
            f"it will not run. (workspace/tools/{stem}.py)"
        )
    deps.extra["reload_pending"] = True
    return (
        f"Tool '{stem}' approved and activated. Run /reload (or it loads next "
        f"session) to call it."
    )


def read_tool(ctx: RunContext[AgentDeps], name: str) -> str:
    """Read back the source of a tool you previously wrote, so you can improve it.

    Returns the code without its provenance header — revise it and pass it
    straight to write_tool with the same name. (Changing the code re-triggers
    validation and a fresh approval, since the content hash changes.)

    Args:
        name: The tool's identifier (as given to write_tool).
    """
    stem = _safe_stem(name)
    if stem is None:
        return f"Error: invalid tool name {name!r}."
    path = ctx.deps.gen_tools_dir / f"{stem}.py"
    if not path.exists():
        return f"Error: no tool named '{stem}'."
    return _strip_provenance(path.read_text(encoding="utf-8"))


#: Registered when ``self_improvement.enabled`` is true.
SELF_IMPROVE_TOOLS = [write_skill, read_skill, remember, write_tool, read_tool]


# ── System-prompt context (skills index + memory digest) ─────────────────────

def skills_overview(workspace) -> str:
    """A one-line-per-skill index for the system prompt (names + summaries)."""
    skills_dir = workspace / "skills"
    if not skills_dir.is_dir():
        return ""
    lines = []
    for md in sorted(skills_dir.glob("*.md")):
        first = ""
        try:
            first = md.read_text(encoding="utf-8").strip().splitlines()[0][:80]
        except Exception:  # noqa: BLE001
            pass
        lines.append(f"- {md.stem}: {first}".rstrip(": "))
    if not lines:
        return ""
    return (
        "Saved skills (call read_skill(name) for the full procedure):\n"
        + "\n".join(lines)
    )


def memory_digest(workspace, limit: int = 5) -> str:
    """The last *limit* recorded lessons, for the system prompt."""
    path = workspace / "memory" / "lessons.jsonl"
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    except Exception:  # noqa: BLE001
        return ""
    lessons = []
    for line in lines:
        try:
            lessons.append(json.loads(line).get("lesson", ""))
        except json.JSONDecodeError:
            continue
    lessons = [lesson for lesson in lessons if lesson]
    if not lessons:
        return ""
    return "Lessons from past sessions:\n" + "\n".join(f"- {lesson}" for lesson in lessons)
