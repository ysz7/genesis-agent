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


def test_read_file_small_is_unpaginated(tmp_path):
    ctx, deps = _ctx(tmp_path)
    try:
        write_file(ctx, "s.txt", "one\ntwo\nthree")
        out = read_file(ctx, "s.txt")
        assert out == "one\ntwo\nthree"          # whole file, no footer (back-compat)
        assert "showing lines" not in out
    finally:
        close_deps(deps)


def test_read_file_offset_limit_windows(tmp_path):
    ctx, deps = _ctx(tmp_path)
    try:
        body = "".join(f"line{i}\n" for i in range(10))
        write_file(ctx, "big.txt", body)
        first = read_file(ctx, "big.txt", limit=3)
        assert first.startswith("line0\nline1\nline2\n")
        assert "showing lines 1-3 of 10" in first and "offset=3" in first
        # The next page is disjoint and ordered, continuing where the footer said.
        second = read_file(ctx, "big.txt", offset=3, limit=3)
        assert second.startswith("line3\nline4\nline5\n")
        assert "showing lines 4-6 of 10" in second
        # Last page reads to the end with no footer.
        last = read_file(ctx, "big.txt", offset=9, limit=3)
        assert last == "line9\n"
        assert "showing lines" not in last
    finally:
        close_deps(deps)


def test_read_file_char_cap_windows_without_limit(tmp_path):
    """With no limit, a big file is still capped to a line window + footer (A1)."""
    ctx, deps = _ctx(tmp_path)
    try:
        deps.settings["max_tool_output"] = 20      # ~3 short lines fit
        write_file(ctx, "cap.txt", "".join(f"row{i}\n" for i in range(20)))
        out = read_file(ctx, "cap.txt")
        assert out.startswith("row0\n")
        assert "of 20" in out and "showing lines 1-" in out
        assert out.count("\n") < 20                 # not the whole file
    finally:
        close_deps(deps)


def test_read_file_offset_beyond_end(tmp_path):
    ctx, deps = _ctx(tmp_path)
    try:
        write_file(ctx, "t.txt", "a\nb\n")
        assert "beyond end" in read_file(ctx, "t.txt", offset=99)
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
