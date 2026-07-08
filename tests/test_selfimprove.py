"""Phase 11: self-improvement — skills, memory, write_tool, approvals, reload."""

import json
from types import SimpleNamespace

from agent.runtime.config import load_config
from agent.runtime.context import build_deps, close_deps
from agent.runtime.approvals import (
    ApprovalStore, content_hash, request_approval, resolve_confirm,
)
from agent.engine.registry import discover_tools, tool_names, _load_generated_tools
from agent.tools import selfimprove as si

_SI = "self_improvement:\n  enabled: true\n"

GOOD_TOOL = 'def greet(name: str) -> str:\n    """Greet a person."""\n    return "hi " + name\n'


def _deps(tmp_path, settings_yaml=_SI, approval="always"):
    (tmp_path / "settings.yaml").write_text(settings_yaml, encoding="utf-8")
    config = load_config(tmp_path)
    deps = build_deps(config)
    if approval is not None:
        deps.approval_hook = lambda subject, detail: approval
    return SimpleNamespace(deps=deps), deps, config


# ── 11d skills ───────────────────────────────────────────────────────────────

def test_skill_round_trip_and_index(tmp_path):
    ctx, deps, config = _deps(tmp_path)
    try:
        assert "Saved skill" in si.write_skill(ctx, "weekly", "# Weekly report\nstep 1\nstep 2")
        assert "step 1" in si.read_skill(ctx, "weekly")
        overview = si.skills_overview(config.workspace)
        assert "weekly" in overview and "Weekly report" in overview
    finally:
        close_deps(deps)


def test_skill_rejects_bad_name(tmp_path):
    ctx, deps, _ = _deps(tmp_path)
    try:
        assert "invalid skill name" in si.write_skill(ctx, "../evil", "x")
    finally:
        close_deps(deps)


# ── 11f reflection memory ─────────────────────────────────────────────────────

def test_remember_appends_and_digest(tmp_path):
    ctx, deps, config = _deps(tmp_path)
    try:
        si.remember(ctx, "Prefer skills over tools.")
        si.remember(ctx, "Always validate inputs.")
        lines = (config.workspace / "memory" / "lessons.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2 and json.loads(lines[0])["lesson"] == "Prefer skills over tools."
        digest = si.memory_digest(config.workspace, 5)
        assert "Always validate inputs." in digest
    finally:
        close_deps(deps)


# ── 11c write_tool: validation gates ──────────────────────────────────────────

def test_write_tool_rejects_syntax_error(tmp_path):
    ctx, deps, _ = _deps(tmp_path)
    try:
        assert "syntax error" in si.write_tool(ctx, "bad", "def x(:\n", "broken")
    finally:
        close_deps(deps)


def test_write_tool_rejects_banned_import(tmp_path):
    ctx, deps, _ = _deps(tmp_path)
    code = 'import subprocess\ndef run(x: str) -> str:\n    """Run."""\n    return x\n'
    try:
        out = si.write_tool(ctx, "run", code, "shell out")
        assert "disallowed" in out and "subprocess" in out
    finally:
        close_deps(deps)


def test_write_tool_rejects_name_escape(tmp_path):
    ctx, deps, _ = _deps(tmp_path)
    try:
        assert "invalid tool name" in si.write_tool(ctx, "../evil", GOOD_TOOL, "x")
    finally:
        close_deps(deps)


def test_write_tool_eval_gate_rejects_contract_violation(tmp_path):
    ctx, deps, config = _deps(tmp_path)
    # Wrong function name + no docstring → fails load/contract; file removed.
    bad = 'def other(x):\n    return x\n'
    try:
        out = si.write_tool(ctx, "greet", bad, "x")
        assert "failed to load or violates the tool contract" in out
        assert not (config.workspace / "tools" / "greet.py").exists()
    finally:
        close_deps(deps)


# ── 11c + 11e: approval-gated activation ──────────────────────────────────────

def test_write_tool_activates_only_after_approval(tmp_path):
    ctx, deps, config = _deps(tmp_path, approval="always")
    try:
        out = si.write_tool(ctx, "greet", GOOD_TOOL, "greet people")
        assert "approved and activated" in out
        assert deps.extra.get("reload_pending") is True
        # The grant persisted, and the approved tool now loads.
        assert ApprovalStore(deps.approvals_path)._grants  # has a grant
        assert "greet" in tool_names(_load_generated_tools(config))
    finally:
        close_deps(deps)


def test_write_tool_declined_does_not_activate(tmp_path):
    ctx, deps, config = _deps(tmp_path, approval="deny")
    try:
        out = si.write_tool(ctx, "greet", GOOD_TOOL, "greet people")
        assert "activation was declined" in out
        assert "greet" not in tool_names(_load_generated_tools(config))  # written, not loaded
    finally:
        close_deps(deps)


def test_generated_tool_reprompts_on_code_change(tmp_path):
    ctx, deps, config = _deps(tmp_path, approval="always")
    try:
        si.write_tool(ctx, "greet", GOOD_TOOL, "v1")
        assert "greet" in tool_names(_load_generated_tools(config))
        # Edit the file after approval — hash changes → no longer loaded.
        path = config.workspace / "tools" / "greet.py"
        path.write_text(path.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
        assert "greet" not in tool_names(_load_generated_tools(config))
    finally:
        close_deps(deps)


# ── 11b: registry integration + timeout wrapper ───────────────────────────────

def test_read_tool_round_trips_without_provenance(tmp_path):
    ctx, deps, config = _deps(tmp_path, approval="always")
    try:
        si.write_tool(ctx, "greet", GOOD_TOOL, "greet people")
        out = si.read_tool(ctx, "greet")
        # The header is stripped, and the code comes back verbatim — so it can
        # be revised and passed straight back to write_tool.
        assert not out.startswith("#")
        assert out == GOOD_TOOL
        assert si.read_tool(ctx, "missing").startswith("Error: no tool")
    finally:
        close_deps(deps)


def test_self_improve_tools_registered_only_when_enabled(tmp_path):
    (tmp_path / "settings.yaml").write_text(_SI, encoding="utf-8")
    names = tool_names(discover_tools(load_config(tmp_path)))
    for t in ("write_skill", "read_skill", "remember", "write_tool", "read_tool"):
        assert t in names

    other = tmp_path / "off"
    other.mkdir()
    (other / "settings.yaml").write_text("name: off\n", encoding="utf-8")
    off_names = tool_names(discover_tools(load_config(other)))
    assert "write_tool" not in off_names


def test_generated_tool_timeout(tmp_path):
    ctx, deps, config = _deps(
        tmp_path, settings_yaml=_SI + "generated_tools:\n  timeout: 0.2\n"
    )
    slow = 'import time\ndef slow(n: int) -> int:\n    """Sleep then return."""\n    time.sleep(1.0)\n    return n\n'
    try:
        si.write_tool(ctx, "slow", slow, "slow tool")
        tools = _load_generated_tools(config)
        slow_fn = next(t for t in tools if t.__name__ == "slow")
        out = slow_fn(n=1)  # no ctx: the tool's signature has none
        assert "exceeded" in str(out) and "abandoned" in str(out)
    finally:
        close_deps(deps)


# ── 11e: approval store + resolve_confirm ──────────────────────────────────────

def test_request_approval_headless_honors_grant_opt_in(tmp_path):
    _, deps, _ = _deps(tmp_path, approval=None)  # no approval_hook → headless
    deps.approval_hook = None
    h = content_hash("code")
    ApprovalStore(deps.approvals_path).grant("tool:x", h)
    try:
        # Headless denies persisted grants by default…
        assert request_approval(deps, "tool:x", h) is False
        # …unless the opt-in is set.
        deps.settings["approvals"] = {"headless_allow_granted": True}
        assert request_approval(deps, "tool:x", h) is True
    finally:
        close_deps(deps)


def test_resolve_confirm_always_persists(tmp_path):
    _, deps, _ = _deps(tmp_path, approval=None)
    calls = {"n": 0}

    def hook(subject, detail):
        calls["n"] += 1
        return "always"

    deps.approval_hook = hook
    try:
        assert resolve_confirm(deps, "run_shell", "echo hi") is True
        assert resolve_confirm(deps, "run_shell", "echo bye") is True  # persisted grant
        assert calls["n"] == 1  # asked once, then remembered
    finally:
        close_deps(deps)
