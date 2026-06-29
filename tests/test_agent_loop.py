"""Phase 8: the agent loop without an LLM — the pattern vertical authors copy.

Build the real agent from a folder (so tool discovery, persona, and settings all
run exactly as in production), then swap the model for ``TestModel`` via
``agent.override(model=...)``. TestModel calls every registered tool once and
returns a canned output — enough to prove the loop wires tools in, invokes one,
and produces a result, all offline and in milliseconds.
"""

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai.messages import ToolCallPart

from agent.runtime.config import load_config
from agent.runtime.context import build_deps, close_deps
from agent.engine.factory import build_agent
from agent.engine.registry import discover_tools, tool_names


def test_agent_loop_calls_vertical_tool(tmp_path):
    # A vertical tool dropped in tools/ — discovered automatically.
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "mathy.py").write_text(
        'def triple(n: int) -> int:\n    """Triple a number."""\n    return n * 3\n',
        encoding="utf-8",
    )
    # Disable the built-ins so TestModel exercises exactly our one tool.
    (tmp_path / "settings.yaml").write_text(
        "tools:\n  disable: [read_file, write_file, list_dir, run_shell, fetch_url, web_search]\n"
        "scheduler:\n  enabled: false\n",
        encoding="utf-8",
    )

    config = load_config(tmp_path)
    assert tool_names(discover_tools(config)) == ["triple"]  # registered

    agent = build_agent(config)
    deps = build_deps(config)
    try:
        # Override the real model with TestModel — no network, deterministic.
        with agent.override(model=TestModel()):
            result = agent.run_sync("do the thing", deps=deps)
    finally:
        close_deps(deps)

    # The loop called our tool…
    called = {
        p.tool_name
        for m in result.all_messages()
        for p in m.parts
        if isinstance(p, ToolCallPart)
    }
    assert "triple" in called
    # …and returned some final output.
    assert result.output
