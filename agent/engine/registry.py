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
from pathlib import Path
from typing import Callable

from pydantic_ai import RunContext

from ..runtime.config import Config
from ..runtime.context import AgentDeps
from ..tools.builtins import BUILTIN_TOOLS


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
        print(f"  ! skipped tools/{py_file.name}: {exc}")
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
        funcs.append(obj)
    return funcs


def discover_tools(config: Config) -> list[Callable]:
    """Return built-in + vertical tools, after applying the ``tools:`` policy.

    ``settings.yaml`` may carry a ``tools:`` block::

        tools:
          disable: [run_shell]              # never registered — model can't see it
          confirm: [run_shell, write_file]  # gated behind deps.confirm_hook

    ``disable`` filters tools out entirely; ``confirm`` wraps them so each call
    is approved by a human first (or refused when no confirmation channel is
    available, e.g. headless serving).
    """
    tools: list[Callable] = list(BUILTIN_TOOLS)

    tools_dir = config.root / "tools"
    if tools_dir.is_dir():
        for py_file in sorted(tools_dir.glob("*.py")):
            if py_file.name.startswith("_"):  # _example.py and friends are patterns
                continue
            tools.extend(_load_module_functions(py_file))

    policy = config.settings.get("tools") or {}
    disable = set(policy.get("disable") or [])
    confirm = set(policy.get("confirm") or [])

    tools = [t for t in tools if getattr(t, "__name__", "") not in disable]
    if confirm:
        tools = [
            _wrap_confirm(t) if getattr(t, "__name__", "") in confirm else t
            for t in tools
        ]
    return tools


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
        ctx = args[0] if args else None
        hook = getattr(getattr(ctx, "deps", None), "confirm_hook", None)
        rendered = _render_call(args[1:], kwargs)
        if hook is None:
            return (
                f"Refused: tool '{name}' is confirm-gated but no confirmation "
                f"channel is available (headless run); it was not executed."
            )
        if not hook(name, rendered):
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
    return wrapper


def _render_call(pos: tuple, kwargs: dict) -> str:
    """A short, human-readable rendering of a tool call's arguments."""
    items = [str(a) for a in pos] + [f"{k}={v}" for k, v in kwargs.items()]
    s = ", ".join(items).replace("\n", " ")
    return s if len(s) <= 300 else s[:299] + "…"


def tool_names(tools: list[Callable]) -> list[str]:
    """Display helper: the registered tool names."""
    return [getattr(t, "__name__", repr(t)) for t in tools]
