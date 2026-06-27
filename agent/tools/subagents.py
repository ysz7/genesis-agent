"""Subagents / delegation (Phase 14 + 17) — opt-in.

Two delegation tools, both running a fresh sub-agent IN-PROCESS on an isolated
subtask (clean message history) and returning just its final answer, so the
parent's context stays lean:

- ``delegate(task)`` — an unnamed sub-agent with the SAME persona as the parent.
- ``delegate_to(name, task)`` — a *named, specialized* sub-agent defined in
  ``workspace/agents/<name>.md`` (its own persona + tool allowance).

Plus authoring tools (gated by ``subagents.allow_authoring``, default on):

- ``write_agent`` / ``read_agent`` — create, improve, or read a subagent
  definition. These write only markdown (a persona + frontmatter), never code.
  Creating a NEW subagent needs no approval (like ``write_skill``); UPDATING an
  existing one — which other runs may rely on — goes through the human approval
  seam (once · always · deny) so a relied-upon specialist isn't silently changed.

A definition is discovered three ways, all converging on the same file: the
agent authors one with ``write_agent``, the user asks it to, or the user drops a
``workspace/agents/<name>.md`` by hand. The roster (name + description) is
injected into the top agent's system prompt so it knows who it can delegate to.

Safety (reused from Phase 14, unchanged):
- **Depth guard** (``subagents.max_depth``, default 1): a sub-agent at the limit
  has neither ``delegate`` nor ``delegate_to`` and is refused at runtime — no
  fork bombs.
- **Per-subagent usage limits** folded back into the parent run so budgets stay
  honest.
- **Restricted toolset**: sub-agents never get ``write_tool``/``write_agent``; a
  named agent's ``tools.allow``/``deny`` can only *narrow* the parent's policy,
  never widen it.

Sub-agents share ``deps`` (store / http / workspace) but NOT message history —
isolation of *context*, not of *state*.
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic_ai import RunContext

from ..runtime.context import AgentDeps

logger = logging.getLogger("agent.subagents")

# Tools a sub-agent never receives, regardless of depth: no self-modifying code
# and no authoring new agents from a delegated context.
_ALWAYS_EXCLUDE = {"write_tool", "write_agent"}

# Marker for a provenance line on agent-authored definitions (an HTML comment so
# it renders invisibly in markdown and is easy to strip on read-back).
_PROVENANCE_MARK = "<!-- genesis-agent: agent-authored subagent"


@dataclasses.dataclass
class SubagentSpec:
    """A parsed ``workspace/agents/<name>.md`` definition."""

    name: str
    description: str
    persona: str
    allow: list[str] | None = None
    deny: list[str] | None = None
    model: str | None = None


# ── Definition files: parse / load ───────────────────────────────────────────

def _safe_name(name: str) -> str | None:
    """A filesystem-safe identifier stem, or None if *name* isn't usable."""
    stem = "".join(c for c in name.strip() if c.isalnum() or c in ("_", "-"))
    if not stem or stem[0].isdigit() or stem != name.strip():
        return None
    return stem


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(frontmatter, body)`` for a ``---``-delimited markdown file.

    No frontmatter (or an unterminated block) → ``("", text)``, so the whole
    file is treated as the persona body.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":  # frontmatter must open at the top
        return "", text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:  # unterminated block — treat the whole file as body
        return "", text
    return "".join(lines[1:end]), "".join(lines[end + 1:])


def _strip_provenance(body: str) -> str:
    """Drop a leading agent-authored provenance comment line, if present."""
    s = body.lstrip("\n")
    if s.startswith(_PROVENANCE_MARK):
        nl = s.find("\n")
        return s[nl + 1:].lstrip("\n") if nl != -1 else ""
    return body


def parse_spec(path: Path) -> SubagentSpec | None:
    """Parse one definition file into a :class:`SubagentSpec`, or None if invalid."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    fm, body = _split_frontmatter(text)
    meta: dict = {}
    if fm.strip():
        try:
            loaded = yaml.safe_load(fm)
        except yaml.YAMLError as exc:
            logger.warning("skipped agents/%s: bad frontmatter — %s", path.name, exc)
            return None
        if isinstance(loaded, dict):
            meta = loaded
    persona = _strip_provenance(body).strip()
    if not persona:
        logger.warning("skipped agents/%s: empty persona body", path.name)
        return None
    tools = meta.get("tools") if isinstance(meta.get("tools"), dict) else {}
    allow = tools.get("allow")
    deny = tools.get("deny")
    model = meta.get("model")
    return SubagentSpec(
        name=path.stem,
        description=str(meta.get("description", "")).strip(),
        persona=persona,
        allow=[str(t) for t in allow] if allow else None,
        deny=[str(t) for t in deny] if deny else None,
        model=str(model).strip() if model else None,
    )


def load_specs(workspace) -> list[SubagentSpec]:
    """Every valid subagent defined under ``workspace/agents/`` (sorted by name)."""
    agents_dir = Path(workspace) / "agents"
    if not agents_dir.is_dir():
        return []
    out: list[SubagentSpec] = []
    for md in sorted(agents_dir.glob("*.md")):
        if md.name.startswith("_"):
            continue
        spec = parse_spec(md)
        if spec is not None:
            out.append(spec)
    return out


def load_spec(workspace, name: str) -> SubagentSpec | None:
    """Load one named subagent, or None if absent / invalid."""
    stem = _safe_name(name)
    if stem is None:
        return None
    path = Path(workspace) / "agents" / f"{stem}.md"
    return parse_spec(path) if path.exists() else None


def agents_overview(workspace) -> str:
    """A one-line-per-agent roster for the system prompt (names + descriptions)."""
    specs = load_specs(workspace)
    if not specs:
        return ""
    lines = [f"- {s.name}: {s.description}".rstrip(": ") for s in specs]
    return (
        "Available subagents — call delegate_to(name, task) to hand one a "
        "self-contained subtask:\n" + "\n".join(lines)
    )


# ── Delegation ───────────────────────────────────────────────────────────────

def _depth(deps: AgentDeps) -> tuple[int, int]:
    """Return ``(current_depth, max_depth)`` from settings + per-run extra."""
    settings = deps.settings.get("subagents") or {}
    return (
        int(deps.extra.get("delegation_depth", 0)),
        int(settings.get("max_depth", 1)),
    )


def _fold_usage(ctx: RunContext[AgentDeps], result) -> None:
    """Fold a sub-agent's token cost into the parent run so limits stay honest."""
    try:
        usage = result.usage
        ctx.usage.incr(usage if hasattr(usage, "input_tokens") else usage())
    except Exception:  # noqa: BLE001 - usage accounting must never break a run
        pass


def _run_subagent(ctx, task, *, exclude, persona_override=None, model_override=None,
                  label="Sub-agent"):
    """Build a fresh sub-agent (isolated history), run *task*, fold usage, return output."""
    from ..engine.factory import build_agent  # local import avoids an import cycle

    deps = ctx.deps
    depth, _ = _depth(deps)
    sub_agent = build_agent(
        deps.config, exclude_tools=exclude, persona_override=persona_override,
        model_override=model_override,
    )
    # Isolated context = fresh run with no message_history. Share state (store,
    # http, workspace); give a fresh `extra` carrying the new depth so a parent's
    # plan / depth don't leak in and concurrent calls don't collide.
    sub_deps = dataclasses.replace(deps, extra={"delegation_depth": depth + 1})
    try:
        result = sub_agent.run_sync(
            task, deps=sub_deps, usage_limits=deps.config.usage_limits
        )
    except Exception as exc:  # noqa: BLE001 - surface the sub-agent's failure
        return f"{label} error: {exc}"
    _fold_usage(ctx, result)
    return str(result.output)


def _base_exclude(depth: int, max_depth: int) -> set[str]:
    """Tools excluded from any sub-agent at this depth (always + depth guard)."""
    exclude = set(_ALWAYS_EXCLUDE)
    if depth + 1 >= max_depth:  # the child would be at the limit — no recursion
        exclude.update({"delegate", "delegate_to"})
    return exclude


def delegate(ctx: RunContext[AgentDeps], task: str) -> str:
    """Delegate a self-contained subtask to a fresh sub-agent; return its answer.

    The sub-agent starts with NO memory of this conversation — it sees only
    ``task`` — so include all the context it needs. Use this to keep your own
    context clean: focused lookups, isolated multi-step subtasks, or splitting a
    job into parts (call it several times). You get back only the final answer.
    For a *specialized* sub-agent (its own persona/tools) use ``delegate_to``.

    Args:
        task: A complete, standalone instruction for the sub-agent.
    """
    depth, max_depth = _depth(ctx.deps)
    if depth >= max_depth:
        return (
            f"Refused: delegation depth limit ({max_depth}) reached — this "
            f"sub-agent cannot delegate further."
        )
    return _run_subagent(ctx, task, exclude=_base_exclude(depth, max_depth))


def delegate_to(ctx: RunContext[AgentDeps], name: str, task: str) -> str:
    """Delegate a subtask to a NAMED specialized sub-agent from workspace/agents/.

    The named agent runs with its own persona and tool allowance (defined in
    ``workspace/agents/<name>.md``) but, like ``delegate``, sees only ``task`` —
    no memory of this conversation — so include everything it needs. Pick the
    agent whose description fits the subtask; you get back only its final answer.

    Args:
        name: The subagent's name (as shown in the roster / its file stem).
        task: A complete, standalone instruction for that sub-agent.
    """
    deps = ctx.deps
    depth, max_depth = _depth(deps)
    if depth >= max_depth:
        return (
            f"Refused: delegation depth limit ({max_depth}) reached — this "
            f"sub-agent cannot delegate further."
        )
    spec = load_spec(deps.workspace, name)
    if spec is None:
        known = ", ".join(s.name for s in load_specs(deps.workspace)) or "(none defined)"
        return f"Error: no subagent named {name!r}. Known: {known}."
    exclude = _base_exclude(depth, max_depth) | _tool_exclusions(deps.config, spec)
    return _run_subagent(
        ctx, task, exclude=exclude, persona_override=spec.persona,
        model_override=spec.model, label=f"Subagent '{spec.name}'",
    )


def _tool_exclusions(config, spec: SubagentSpec) -> set[str]:
    """Translate a spec's allow/deny into an exclude set — allow only NARROWS.

    A ``deny`` list is excluded outright. An ``allow`` list excludes everything
    the parent has that isn't on it; a parent-``disable``d tool isn't in the
    parent's set to begin with, so ``allow`` can never re-grant it.
    """
    exclude: set[str] = set(spec.deny or [])
    if spec.allow is not None:
        from ..engine.registry import discover_tools, tool_names

        available = set(tool_names(discover_tools(config)))
        exclude |= available - set(spec.allow)
    return exclude


# ── Authoring (markdown only — no approval, like write_skill) ─────────────────

def _provenance(model: str) -> str:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"{_PROVENANCE_MARK}; created {now}; model {model} -->\n"


def write_agent(
    ctx: RunContext[AgentDeps],
    name: str,
    description: str,
    persona: str,
    tools: list[str] | None = None,
) -> str:
    """Create or improve a named, specialized subagent for later delegation.

    Saves ``workspace/agents/<name>.md`` (frontmatter + persona). It's a prompt,
    not code, so it activates immediately — call ``delegate_to(name, task)`` to
    use it. **Creating a new subagent is free; updating an existing one asks the
    human first** (other runs may rely on it). To improve one, ``read_agent`` it,
    revise, then ``write_agent`` the same name.

    Args:
        name: Identifier (letters, digits, ``_`` or ``-``) — the file stem.
        description: One line on what this subagent is for; shown in the roster
            so you know when to delegate to it.
        persona: The subagent's system prompt — who it is and how it works.
        tools: Optional allow-list of tool names it may use (omit to inherit
            yours). It can only narrow your toolset, never widen it.
    """
    deps = ctx.deps
    stem = _safe_name(name)
    if stem is None:
        return f"Error: invalid subagent name {name!r} (use letters, digits, _ or -)."
    if not persona.strip():
        return "Error: persona is empty — describe who the subagent is."
    meta: dict = {"description": description.strip()}
    if tools:
        meta["tools"] = {"allow": list(tools)}
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    full = f"---\n{front}\n---\n{_provenance(deps.config.model)}{persona.strip()}\n"
    path = deps.agents_dir / f"{stem}.md"

    # Creating a new subagent is free (markdown, like write_skill); EDITING an
    # existing one — which other runs may already depend on — is gated by the
    # human approval seam (once · always · deny; headless denies by default).
    if path.exists():
        from ..runtime.approvals import content_hash, request_approval

        if not request_approval(
            deps, f"agent:{stem}", content_hash(full),
            detail=f"update existing subagent '{stem}': {description.strip()}",
        ):
            return (
                f"Update to subagent '{stem}' was declined — left unchanged "
                f"(workspace/agents/{stem}.md)."
            )
        verb = "Updated"
    else:
        verb = "Saved"
    path.write_text(full, encoding="utf-8")
    return (
        f"{verb} subagent '{stem}' (workspace/agents/{stem}.md). "
        f"Use it with delegate_to('{stem}', task)."
    )


def read_agent(ctx: RunContext[AgentDeps], name: str) -> str:
    """Read back a subagent definition (frontmatter + persona) to inspect or revise it.

    Returns the file without its provenance header; edit it and pass the pieces
    back to ``write_agent`` with the same name to overwrite.

    Args:
        name: The subagent's name (as given to write_agent).
    """
    stem = _safe_name(name)
    if stem is None:
        return f"Error: invalid subagent name {name!r}."
    path = ctx.deps.agents_dir / f"{stem}.md"
    if not path.exists():
        return f"Error: no subagent named '{stem}'."
    fm, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    return f"---\n{fm.strip()}\n---\n{_strip_provenance(body).strip()}"


#: Delegation tools — registered when ``subagents.enabled`` is true.
SUBAGENT_TOOLS = [delegate, delegate_to]
#: Authoring tools — added when ``subagents.allow_authoring`` is true (default).
AUTHORING_TOOLS = [write_agent, read_agent]
