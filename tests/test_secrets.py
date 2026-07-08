"""Phase 27a: secret redaction — .env values scrubbed from tool output + answer."""

from types import SimpleNamespace

from agent.runtime.config import load_config
from agent.runtime.context import build_deps, close_deps
from agent.runtime import secrets as secrets_mod
from agent.engine.registry import discover_tools


def _find(tools, name):
    return next(t for t in tools if getattr(t, "__name__", "") == name)


def test_collect_secrets_reads_env_values(tmp_path):
    (tmp_path / ".env").write_text(
        "PROVIDER=openai\nMODEL=gpt-4o\nAPI_KEY=sk-supersecretvalue123\n"
        "BASE_URL=https://api.openai.com/v1\nSHORT=abc\n",
        encoding="utf-8",
    )
    found = secrets_mod.collect_secrets(tmp_path)
    assert found == {"API_KEY": "sk-supersecretvalue123"}  # PROVIDER/MODEL/BASE_URL/SHORT excluded


def test_collect_secrets_skips_url_without_credentials(tmp_path):
    (tmp_path / ".env").write_text(
        "WEBHOOK_URL=https://example.com/hooks/abc\n"
        "DB_URL=postgres://user:hunter2pass@host/db\n",
        encoding="utf-8",
    )
    found = secrets_mod.collect_secrets(tmp_path)
    assert "WEBHOOK_URL" not in found
    assert found["DB_URL"] == "postgres://user:hunter2pass@host/db"


def test_redact_text_replaces_all_values():
    secrets = {"API_KEY": "sk-abc12345", "TOKEN": "tok-98765432"}
    text = "key is sk-abc12345 and token is tok-98765432 (repeated sk-abc12345)"
    out = secrets_mod.redact_text(text, secrets)
    assert "sk-abc12345" not in out and "tok-98765432" not in out
    assert "[secret:API_KEY]" in out and "[secret:TOKEN]" in out


def test_redact_value_recurses_into_list_and_dict():
    secrets = {"API_KEY": "sk-abc12345"}
    value = {"a": ["sk-abc12345", "clean"], "b": "sk-abc12345"}
    out = secrets_mod.redact_value(value, secrets)
    assert out == {"a": ["[secret:API_KEY]", "clean"], "b": "[secret:API_KEY]"}


def test_run_shell_leak_is_redacted(tmp_path):
    (tmp_path / ".env").write_text(
        "PROVIDER=openai\nMODEL=gpt-4o\nAPI_KEY=sk-supersecretvalue123\n",
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    run_shell = _find(discover_tools(config), "run_shell")
    deps = build_deps(config)
    assert deps.secrets == {"API_KEY": "sk-supersecretvalue123"}
    deps.confirm_hook = lambda name, args: True  # run_shell is confirm-gated by default
    ctx = SimpleNamespace(deps=deps)
    try:
        out = run_shell(ctx, command="echo sk-supersecretvalue123")
        assert "sk-supersecretvalue123" not in out
        assert "[secret:API_KEY]" in out
    finally:
        close_deps(deps)


def test_read_file_leak_is_redacted(tmp_path):
    (tmp_path / ".env").write_text(
        "PROVIDER=openai\nMODEL=gpt-4o\nAPI_KEY=sk-supersecretvalue123\n",
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    read_file_tool = _find(discover_tools(config), "read_file")
    deps = build_deps(config)
    (deps.files_dir / "leak.txt").write_text("token: sk-supersecretvalue123", encoding="utf-8")
    ctx = SimpleNamespace(deps=deps)
    try:
        out = read_file_tool(ctx, "leak.txt")
        assert "sk-supersecretvalue123" not in out
        assert "[secret:API_KEY]" in out
    finally:
        close_deps(deps)


def test_redact_secrets_false_disables_collection(tmp_path):
    (tmp_path / ".env").write_text(
        "PROVIDER=openai\nMODEL=gpt-4o\nAPI_KEY=sk-supersecretvalue123\n",
        encoding="utf-8",
    )
    (tmp_path / "settings.yaml").write_text("redact_secrets: false\n", encoding="utf-8")
    config = load_config(tmp_path)
    deps = build_deps(config)
    try:
        assert deps.secrets == {}
    finally:
        close_deps(deps)


def test_confirm_run_shell_is_the_shipped_default(tmp_path):
    """The template settings.yaml ships tools.confirm: [run_shell] uncommented."""
    import yaml
    from pathlib import Path

    template = Path(__file__).resolve().parent.parent / "settings.yaml"
    data = yaml.safe_load(template.read_text(encoding="utf-8"))
    assert data.get("tools", {}).get("confirm") == ["run_shell"]
