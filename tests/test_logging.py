"""Phase 5: logging & observability — no prints in the engine, run log, degradation."""

import json
import re
from pathlib import Path

import agent as agent_pkg
from agent.engine import factory
from agent.runtime.config import load_config
from agent.runtime.context import build_deps, close_deps
from agent.runtime.runlog import append_run


def test_no_bare_print_in_engine_and_runtime():
    """Architectural rule: engine/ and runtime/ log via logging.getLogger('agent.*'),
    never print. (console/ owns rich rendering; server's startup prints are UX.)"""
    root = Path(agent_pkg.__file__).parent
    offenders = []
    for sub in ("engine", "runtime"):
        for py in (root / sub).glob("*.py"):
            for n, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                if line.strip().startswith("#"):
                    continue
                if re.search(r"(?<![\w.])print\(", line):
                    offenders.append(f"{sub}/{py.name}:{n}")
    assert not offenders, f"bare print() in engine/runtime: {offenders}"


def test_runlog_appends_jsonl(tmp_path):
    (tmp_path / "settings.yaml").write_text("log_runs: true\n", encoding="utf-8")
    deps = build_deps(load_config(tmp_path))
    try:
        append_run(deps, "demo task", 1.234, 42, ok=True)
        append_run(deps, "bad task", 0.5, 0, ok=False, error="boom")
    finally:
        close_deps(deps)

    lines = (tmp_path / "workspace" / "runs.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first, second = (json.loads(l) for l in lines)
    assert first == {**first, "task": "demo task", "duration_s": 1.23, "tokens": 42, "ok": True}
    assert "ts" in first and "error" not in first
    assert second["ok"] is False and second["error"] == "boom"


def test_runlog_disabled_writes_nothing(tmp_path):
    deps = build_deps(load_config(tmp_path))  # no log_runs in settings
    try:
        append_run(deps, "demo", 1.0, 1, ok=True)
    finally:
        close_deps(deps)
    assert not (tmp_path / "workspace" / "runs.jsonl").exists()


def test_observability_degrades_without_token(monkeypatch):
    """No LOGFIRE_TOKEN → _setup_observability is a no-op and never raises."""
    monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
    monkeypatch.setattr(factory, "_observability_done", False)
    factory._setup_observability()  # must not raise (logfire likely not installed)
    assert factory._observability_done is True
