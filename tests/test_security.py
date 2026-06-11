"""Phase 2 security hardening: workspace sandbox + tool policy (disable/confirm).

The builtins only read ``ctx.deps.workspace`` / ``ctx.deps.settings`` and the
confirm wrapper only reads ``ctx.deps.confirm_hook``, so a tiny ``SimpleNamespace``
standing in for ``RunContext`` is enough to exercise them directly.
"""

from types import SimpleNamespace

from agent.runtime.config import load_config
from agent.runtime.context import build_deps, close_deps
from agent.engine.registry import discover_tools, tool_names
from agent.tools.builtins import read_file, write_file, list_dir


def _ctx(tmp_path):
    deps = build_deps(load_config(tmp_path))
    return SimpleNamespace(deps=deps), deps


# ── 2a. Workspace sandbox ────────────────────────────────────────────────────

def test_sandbox_allows_inside_workspace(tmp_path):
    ctx, deps = _ctx(tmp_path)
    try:
        assert write_file(ctx, "note.txt", "hi").startswith("Wrote")
        assert read_file(ctx, "note.txt") == "hi"
    finally:
        close_deps(deps)


def test_sandbox_blocks_relative_escape(tmp_path):
    ctx, deps = _ctx(tmp_path)
    try:
        assert "escapes the workspace" in read_file(ctx, "../secret.txt")
        # list_dir returns a list, but the error still surfaces.
        assert any("escapes the workspace" in e for e in list_dir(ctx, ".."))
    finally:
        close_deps(deps)


def test_sandbox_blocks_absolute_escape(tmp_path):
    ctx, deps = _ctx(tmp_path)
    outside = str(tmp_path / "outside.txt")  # sibling of workspace/, not within it
    try:
        assert "escapes the workspace" in write_file(ctx, outside, "x")
        assert not (tmp_path / "outside.txt").exists()  # nothing was written
    finally:
        close_deps(deps)


def test_sandbox_off_allows_escape(tmp_path):
    ctx, deps = _ctx(tmp_path)
    deps.settings["sandbox"] = False
    try:
        # No longer an "escapes" error — the path is resolved as given (and
        # simply doesn't exist yet).
        assert "escapes the workspace" not in read_file(ctx, "../nope.txt")
    finally:
        close_deps(deps)


# ── 2b. Tool policy ──────────────────────────────────────────────────────────

def test_disable_removes_tool(tmp_path):
    (tmp_path / "settings.yaml").write_text(
        "tools:\n  disable: [run_shell]\n", encoding="utf-8"
    )
    names = tool_names(discover_tools(load_config(tmp_path)))
    assert "run_shell" not in names
    assert "read_file" in names  # others untouched


def _find(tools, name):
    return next(t for t in tools if getattr(t, "__name__", "") == name)


def test_confirm_refuses_without_hook(tmp_path):
    (tmp_path / "settings.yaml").write_text(
        "tools:\n  confirm: [run_shell]\n", encoding="utf-8"
    )
    config = load_config(tmp_path)
    tools = discover_tools(config)
    run_shell = _find(tools, "run_shell")  # the confirm-wrapped version

    deps = build_deps(config)
    deps.confirm_hook = None  # headless: no human
    ctx = SimpleNamespace(deps=deps)
    marker = config.workspace / "ran.txt"
    try:
        out = run_shell(ctx, command=f"echo hi > {marker.name}")
        assert "Refused" in out
        assert not marker.exists()  # the command never ran
    finally:
        close_deps(deps)


def test_confirm_runs_when_approved(tmp_path):
    (tmp_path / "settings.yaml").write_text(
        "tools:\n  confirm: [run_shell]\n", encoding="utf-8"
    )
    config = load_config(tmp_path)
    run_shell = _find(discover_tools(config), "run_shell")

    deps = build_deps(config)
    seen: list[tuple[str, str]] = []
    deps.confirm_hook = lambda name, args: seen.append((name, args)) or True
    ctx = SimpleNamespace(deps=deps)
    try:
        out = run_shell(ctx, command="echo confirmed")
        assert "confirmed" in out
        assert seen and seen[0][0] == "run_shell"  # hook was consulted
    finally:
        close_deps(deps)
