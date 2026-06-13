"""Phase 8: built-in tool unit tests (tmp workspace, no LLM).

Sandbox-escape and fetch_url HTML cleaning have dedicated coverage in
test_security.py / test_tool_quality.py; this file covers the round-trip and
the run_shell paths.
"""

import sys
from types import SimpleNamespace

from agent.runtime.config import load_config
from agent.runtime.context import build_deps, close_deps
from agent.tools.builtins import list_dir, read_file, run_shell, write_file


def _ctx(tmp_path):
    deps = build_deps(load_config(tmp_path))
    return SimpleNamespace(deps=deps), deps


def test_write_read_list_round_trip(tmp_path):
    ctx, deps = _ctx(tmp_path)
    try:
        assert write_file(ctx, "notes/a.txt", "hello").startswith("Wrote")
        assert read_file(ctx, "notes/a.txt") == "hello"      # parent dir auto-created
        assert "a.txt" in list_dir(ctx, "notes")
        assert "notes/" in list_dir(ctx, ".")                # dirs suffixed with /
    finally:
        close_deps(deps)


def test_read_missing_file(tmp_path):
    ctx, deps = _ctx(tmp_path)
    try:
        assert "not found" in read_file(ctx, "nope.txt")
    finally:
        close_deps(deps)


def test_run_shell_reports_exit_code(tmp_path):
    ctx, deps = _ctx(tmp_path)
    try:
        out = run_shell(ctx, sys.executable + ' -c "import sys; sys.exit(3)"')
        assert "[exit 3]" in out
    finally:
        close_deps(deps)


def test_run_shell_timeout(tmp_path):
    ctx, deps = _ctx(tmp_path)
    try:
        # Use the running interpreter so the command is portable across shells.
        cmd = f'"{sys.executable}" -c "import time; time.sleep(5)"'
        out = run_shell(ctx, cmd, timeout=1)
        assert "timed out after 1s" in out
    finally:
        close_deps(deps)
