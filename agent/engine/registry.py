"""Tool discovery: built-ins + auto-discovered ``tools/*.py``.

Collects the built-in tools and every module-level function in the agent
folder's ``tools/`` directory that looks like a tool (has a docstring, is
defined in that module, and isn't underscore-prefixed). The returned list is
passed straight to ``Agent(tools=...)``; Pydantic AI derives each schema from
the signature, so dropping a new ``tools/foo.py`` is all it takes to add a tool.

A tool reaches shared deps by taking ``RunContext[AgentDeps]`` as its first
parameter — Pydantic AI detects that automatically.
"""

from __future__ import annotations

import functools
import importlib.util
import inspect
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Callable

from pydantic_ai import RunContext

from ..runtime.config import Config
from ..runtime.context import AgentDeps
from ..tools.builtins import BUILTIN_TOOLS

logger = logging.getLogger("agent.registry")


def _load_module_functions(py_file: Path) -> list[Callable]:
    """Import *py_file* in isolation and return its tool-like functions."""
    mod_name = f"_genesisagent_tools_{py_file.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, py_file)
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - one bad tool file shouldn't kill startup
        logger.warning("skipped tools/%s: %s", py_file.name, exc)
        return []

    funcs: list[Callable] = []
    for name, obj in vars(module).items():
        if name.startswith("_"):
            continue
        if not inspect.isfunction(obj):
            continue
        if obj.__module__ != mod_name:  # ignore imported helpers
            continue
        if not (obj.__doc__ or "").strip():  # tools must be documented
            continue
        bad = _unannotated_param(obj)
        if bad is not None:
            logger.warning(
                "skipped tools/%s:%s — parameter '%s' has no type annotation "
                "(it would degrade the model-facing schema)",
                py_file.name, name, bad,
            )
            continue
        funcs.append(obj)
    return funcs


def _unannotated_param(func: Callable) -> str | None:
    """Name of the first non-ctx, un-annotated parameter, or None if all are fine.

    The ``RunContext`` parameter is exempt (Pydantic AI injects it); ``*args`` /
    ``**kwargs`` are ignored. Every other parameter must carry a type annotation
    so the generated tool schema is meaningful.
    """
    for param in inspect.signature(func).parameters.values():
        ann = param.annotation
        if ann is not inspect.Parameter.empty and "RunContext" in str(ann):
            continue  # the deps context — not part of the model schema
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if ann is inspect.Parameter.empty:
            return param.name
    return None


def discover_tools(config: Config, exclude: set[str] | None = None) -> list[Callable]:
    """Return built-in + vertical tools, after applying the ``tools:`` policy.

    ``settings.yaml`` may carry a ``tools:`` block::

        tools:
          disable: [run_shell]              # never registered — model can't see it
          confirm: [run_shell, write_file]  # gated behind deps.confirm_hook

    ``disable`` filters tools out entirely; ``confirm`` wraps them so each call
    is approved by a human first (or refused when no confirmation channel is
    available, e.g. headless serving). *exclude* is a runtime addition to
    ``disable`` (Phase 14 uses it to build a restricted sub-agent toolset).
    """
    tools: list[Callable] = list(BUILTIN_TOOLS)

    tools_dir = config.root / "tools"
    if tools_dir.is_dir():
        for py_file in sorted(tools_dir.glob("*.py")):
            if py_file.name.startswith("_"):  # _example.py and friends are patterns
                continue
            tools.extend(_load_module_functions(py_file))

    # Explicit planning (Phase 13): an opt-in todo-scratchpad tool.
    if (config.settings.get("planning") or {}).get("enabled"):
        from ..tools.planning import PLANNING_TOOLS

        tools.extend(PLANNING_TOOLS)

    # Scheduling (Phase 23): recurring tasks the agent creates from a chat. Same
    # opt-in pattern as planning/subagents: OFF at the code level, turned on by
    # the template settings.yaml (`scheduler.enabled: true`) — so an agent with a
    # minimal/older settings file doesn't silently grow tools. Jobs fire in the
    # background while the server or a gateway bot is running.
    if (config.settings.get("scheduler") or {}).get("enabled"):
        from ..tools.scheduling import SCHEDULING_TOOLS

        tools.extend(SCHEDULING_TOOLS)

    # Subagents (Phase 14 + 17): opt-in delegation + named-agent authoring.
    sub = config.settings.get("subagents") or {}
    if sub.get("enabled"):
        from ..tools.subagents import SUBAGENT_TOOLS

        tools.extend(SUBAGENT_TOOLS)
        # Authoring writes markdown only (no code), so it's on by default like
        # write_skill; set subagents.allow_authoring: false for a fixed roster.
        if sub.get("allow_authoring", True):
            from ..tools.subagents import AUTHORING_TOOLS

            tools.extend(AUTHORING_TOOLS)

    # Self-improvement (Phase 11): the agent's own authoring tools, plus any
    # generated tools it has written AND a human has approved. Opt-in, and
    # added after human-authored tools so those win on name collisions.
    if (config.settings.get("self_improvement") or {}).get("enabled"):
        from ..tools.selfimprove import SELF_IMPROVE_TOOLS

        tools.extend(SELF_IMPROVE_TOOLS)
        tools.extend(_load_generated_tools(config))

    # Drop duplicate names: builtins win, then earlier files. Registering two
    # tools with one name makes Pydantic AI error or silently shadow.
    seen: set[str] = set()
    deduped: list[Callable] = []
    for tool in tools:
        name = getattr(tool, "__name__", "")
        if name in seen:
            logger.warning(
                "duplicate tool name %r — keeping the first, skipping this one", name
            )
            continue
        seen.add(name)
        deduped.append(tool)
    tools = deduped

    policy = config.settings.get("tools") or {}
    disable = set(policy.get("disable") or []) | (exclude or set())
    confirm = set(policy.get("confirm") or [])

    tools = [t for t in tools if getattr(t, "__name__", "") not in disable]
    if confirm:
        tools = [
            _wrap_confirm(t) if getattr(t, "__name__", "") in confirm else t
            for t in tools
        ]

    # Secret redaction (Phase 27a): scrub .env values from every tool's output
    # before the model sees it. Outermost wrapper, so confirm refusals and
    # generated-tool errors pass through it too. ON unless redact_secrets: false.
    from ..runtime import secrets as secrets_mod

    if secrets_mod.enabled(config.settings):
        tools = [_wrap_redact(t) for t in tools]
    return tools


def _wrap_redact(tool: Callable) -> Callable:
    """Scrub ``deps.secrets`` values from *tool*'s return value.

    Same signature-preserving technique as :func:`_wrap_confirm`: the wrapper
    always has a ``RunContext`` first parameter (the tool's own, or an injected
    one) so it can reach ``deps.secrets``, and ``__signature__`` keeps the
    model-facing schema derived from the original signature.
    """
    from ..runtime.secrets import redact_value

    sig = inspect.signature(tool)
    params = list(sig.parameters.values())
    has_ctx = bool(params) and "RunContext" in str(params[0].annotation)

    def _scrub(ctx, out):
        deps = getattr(ctx, "deps", None)
        secrets = getattr(deps, "secrets", None) or {}
        return redact_value(out, secrets) if secrets else out

    if inspect.iscoroutinefunction(tool):
        @functools.wraps(tool)
        async def wrapper(*args, **kwargs):
            ctx = args[0] if args else None
            call_args = args if has_ctx else args[1:]
            return _scrub(ctx, await tool(*call_args, **kwargs))
    else:
        @functools.wraps(tool)
        def wrapper(*args, **kwargs):
            ctx = args[0] if args else None
            call_args = args if has_ctx else args[1:]
            return _scrub(ctx, tool(*call_args, **kwargs))

    if has_ctx:
        wrapper.__signature__ = sig  # type: ignore[attr-defined]
    else:
        ctx_param = inspect.Parameter(
            "_ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=RunContext[AgentDeps],
        )
        wrapper.__signature__ = sig.replace(parameters=[ctx_param, *params])  # type: ignore[attr-defined]
        # get_type_hints() (used by Pydantic AI's schema builder) reads
        # __annotations__ directly rather than the signature — functools.wraps
        # copied the ORIGINAL tool's annotations, which have no "_ctx" entry,
        # so add it or schema generation KeyErrors on this synthetic parameter.
        wrapper.__annotations__ = {"_ctx": RunContext[AgentDeps], **getattr(tool, "__annotations__", {})}
    return wrapper


def _wrap_confirm(tool: Callable) -> Callable:
    """Gate *tool* behind ``ctx.deps.confirm_hook`` without altering its schema.

    ``functools.wraps`` + an explicit ``__signature__`` keep Pydantic AI deriving
    the model-facing schema from the original signature. The wrapper always has a
    ``RunContext`` first parameter (the tool's own, or one we inject) so it can
    reach ``deps.confirm_hook`` — Pydantic AI passes that context positionally and
    hides it from the model.
    """
    sig = inspect.signature(tool)
    params = list(sig.parameters.values())
    has_ctx = bool(params) and "RunContext" in str(params[0].annotation)
    name = getattr(tool, "__name__", "tool")

    @functools.wraps(tool)
    def wrapper(*args, **kwargs):
        from ..runtime.approvals import resolve_confirm

        ctx = args[0] if args else None
        deps = getattr(ctx, "deps", None)
        rendered = _render_call(args[1:], kwargs)
        if deps is None or (deps.approval_hook is None and deps.confirm_hook is None):
            return (
                f"Refused: tool '{name}' is confirm-gated but no confirmation "
                f"channel is available (headless run); it was not executed."
            )
        if not resolve_confirm(deps, name, rendered):
            return f"Refused: '{name}' was declined by the operator; not executed."
        call_args = args if has_ctx else args[1:]
        return tool(*call_args, **kwargs)

    if has_ctx:
        wrapper.__signature__ = sig  # type: ignore[attr-defined]
    else:
        ctx_param = inspect.Parameter(
            "_ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=RunContext[AgentDeps],
        )
        wrapper.__signature__ = sig.replace(parameters=[ctx_param, *params])  # type: ignore[attr-defined]
        # get_type_hints() (used by Pydantic AI's schema builder) reads
        # __annotations__ directly rather than the signature — functools.wraps
        # copied the ORIGINAL tool's annotations, which have no "_ctx" entry,
        # so add it or schema generation KeyErrors on this synthetic parameter.
        wrapper.__annotations__ = {"_ctx": RunContext[AgentDeps], **getattr(tool, "__annotations__", {})}
    return wrapper


def _render_call(pos: tuple, kwargs: dict) -> str:
    """A short, human-readable rendering of a tool call's arguments."""
    items = [str(a) for a in pos] + [f"{k}={v}" for k, v in kwargs.items()]
    s = ", ".join(items).replace("\n", " ")
    return s if len(s) <= 300 else s[:299] + "…"


def _load_generated_tools(config: Config) -> list[Callable]:
    """Load agent-written tools from ``workspace/tools/`` — approved files only.

    Each file's content is hashed and checked against the persisted approval
    grants (Phase 11e): an unapproved file, or one edited since approval (hash
    changed), is skipped. Loaded tools are wrapped with a wall-clock timeout
    (containment against accidental infinite loops, not a security boundary —
    the human approval is the boundary).
    """
    from ..runtime.approvals import ApprovalStore, content_hash

    gen_dir = config.workspace / "tools"
    if not gen_dir.is_dir():
        return []
    store = ApprovalStore(config.workspace / "approvals.json")
    timeout = float((config.settings.get("generated_tools") or {}).get("timeout", 10))

    out: list[Callable] = []
    for py_file in sorted(gen_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            src = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        if not store.is_granted(f"tool:{py_file.stem}", content_hash(src)):
            logger.info(
                "generated tool %s is unapproved or changed — not loaded", py_file.name
            )
            continue
        for fn in _load_module_functions(py_file):
            out.append(_wrap_timeout(fn, timeout))
    return out


def _wrap_timeout(tool: Callable, timeout: float) -> Callable:
    """Run *tool* under a wall-clock *timeout*; on overrun return an error string.

    Schema is preserved (``__signature__``) so Pydantic AI still injects ctx and
    builds the model-facing schema from the original signature. An over-running
    call is abandoned (its thread may linger — accidents containment, not a kill).
    """
    @functools.wraps(tool)
    def wrapper(*args, **kwargs):
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(tool, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeout:
            return (
                f"Error: generated tool '{tool.__name__}' exceeded {timeout:.0f}s "
                f"and was abandoned."
            )
        finally:
            executor.shutdown(wait=False)

    wrapper.__signature__ = inspect.signature(tool)  # type: ignore[attr-defined]
    return wrapper


def tool_names(tools: list[Callable]) -> list[str]:
    """Display helper: the registered tool names."""
    return [getattr(t, "__name__", repr(t)) for t in tools]
