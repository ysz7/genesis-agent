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

import importlib.util
import inspect
from pathlib import Path
from typing import Callable

from ..runtime.config import Config
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
    """Return built-in tools followed by auto-discovered vertical tools."""
    tools: list[Callable] = list(BUILTIN_TOOLS)

    tools_dir = config.root / "tools"
    if tools_dir.is_dir():
        for py_file in sorted(tools_dir.glob("*.py")):
            if py_file.name.startswith("_"):  # _example.py and friends are patterns
                continue
            tools.extend(_load_module_functions(py_file))
    return tools


def tool_names(tools: list[Callable]) -> list[str]:
    """Display helper: the registered tool names."""
    return [getattr(t, "__name__", repr(t)) for t in tools]
