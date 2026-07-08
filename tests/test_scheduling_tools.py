"""Phase 23b: the agent-facing scheduling tools, over a fake ctx + real store."""

from types import SimpleNamespace

from agent.runtime import scheduler
from agent.runtime.store import open_store
from agent.tools import scheduling
from agent.engine.registry import discover_tools, tool_names
from agent.runtime.config import load_config


def _ctx(store, *, settings=None, origin=None):
    deps = SimpleNamespace(store=store, settings=settings or {}, extra={})
    if origin is not None:
        deps.extra["channel_origin"] = origin
    return SimpleNamespace(deps=deps)


def test_schedule_then_list(tmp_path):
    store = open_store(tmp_path / "s.json")
    ctx = _ctx(store, origin={"channel": "telegram", "user": "7"})
    out = scheduling.schedule_task(ctx, "summarize HN", "2h")
    assert "every 2h" in out
    jobs = scheduler.list_jobs(store)
    assert jobs[0]["task"] == "summarize HN" and jobs[0]["origin"]["user"] == "7"
    listing = scheduling.list_scheduled(ctx)
    assert jobs[0]["id"] in listing and "summarize HN" in listing
    store.close()


def test_schedule_bad_interval(tmp_path):
    store = open_store(tmp_path / "s.json")
    out = scheduling.schedule_task(_ctx(store), "x", "whenever")
    assert "Couldn't schedule" in out
    assert scheduler.list_jobs(store) == []
    store.close()


def test_max_jobs_enforced(tmp_path):
    store = open_store(tmp_path / "s.json")
    ctx = _ctx(store, settings={"scheduler": {"max_jobs": 1}})
    scheduling.schedule_task(ctx, "a", "1h")
    out = scheduling.schedule_task(ctx, "b", "1h")
    assert "Couldn't schedule" in out and "max 1" in out
    store.close()


def test_edit_and_cancel(tmp_path):
    store = open_store(tmp_path / "s.json")
    ctx = _ctx(store)
    scheduling.schedule_task(ctx, "ping", "1h")
    jid = scheduler.list_jobs(store)[0]["id"]
    assert "every 30m" in scheduling.edit_scheduled(ctx, jid, every="30m")
    assert scheduler.get_job(store, jid)["every"] == 1800
    assert "No scheduled task" in scheduling.edit_scheduled(ctx, "zzzz", task="x")
    assert "Cancelled" in scheduling.cancel_scheduled(ctx, jid)
    assert "No scheduled task" in scheduling.cancel_scheduled(ctx, jid)
    store.close()


def test_list_empty(tmp_path):
    store = open_store(tmp_path / "s.json")
    assert "No scheduled tasks" in scheduling.list_scheduled(_ctx(store))
    store.close()


# ── registry wiring ───────────────────────────────────────────────────────────

def test_tools_registered_when_enabled(tmp_path):
    (tmp_path / "settings.yaml").write_text("scheduler:\n  enabled: true\n", encoding="utf-8")
    names = tool_names(discover_tools(load_config(tmp_path)))
    for t in ("schedule_task", "list_scheduled", "cancel_scheduled", "edit_scheduled"):
        assert t in names


def test_tools_off_by_code_default(tmp_path):
    # Same opt-in pattern as planning/subagents: OFF in code, ON in the template
    # settings.yaml — a minimal settings file must not silently grow tools.
    names = tool_names(discover_tools(load_config(tmp_path)))    # no settings at all
    assert "schedule_task" not in names


def test_template_settings_enable_scheduler():
    # The shipped template turns the scheduler on (out-of-box behaviour).
    import pathlib
    import yaml

    template = pathlib.Path(__file__).resolve().parents[1] / "settings.yaml"
    settings = yaml.safe_load(template.read_text(encoding="utf-8"))
    assert (settings.get("scheduler") or {}).get("enabled") is True
