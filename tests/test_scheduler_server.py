"""Phase 23f: the server's background scheduler ticker fires due jobs."""

import socket
import time

from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.messages import ModelResponse, TextPart

from agent.engine import factory
from agent.runtime import scheduler
from agent.runtime.config import load_config
from agent.runtime.store import open_store
from agent.server import start_background, stop_background


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _model() -> FunctionModel:
    def fn(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content="tick-done")])
    return FunctionModel(fn)


def test_server_runs_due_job(monkeypatch, tmp_path):
    (tmp_path / "settings.yaml").write_text(
        "store: agent.sqlite\nscheduler:\n  enabled: true\n  tick: 1\n", encoding="utf-8"
    )
    monkeypatch.setattr(factory, "build_model", lambda config: _model())
    config = load_config(tmp_path)

    # seed a due job in the same store the server will open
    store = open_store(config.workspace / "agent.sqlite")
    job = scheduler.add_job(store, "do the thing", 3600)
    jobs = scheduler.list_jobs(store)
    jobs[0]["next_run"] = 1                       # force due immediately
    store.set(scheduler.KEY, jobs)
    store.close()

    httpd, deps = start_background(config, port=_free_port())
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if (scheduler.get_job(deps.store, job["id"]) or {}).get("runs", 0) >= 1:
                break
            time.sleep(0.2)
        fired = scheduler.get_job(deps.store, job["id"])
        assert fired["runs"] >= 1                 # the ticker ran it
        assert fired["next_run"] > time.time()    # and rescheduled it
    finally:
        stop_background(httpd, deps)
