"""Phase 28: run transcripts — opt-in full trace, redaction, pruning."""

import json
from types import SimpleNamespace

from pydantic_ai.messages import (
    ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart,
)

from agent.runtime.config import load_config
from agent.runtime.context import build_deps, close_deps
from agent.runtime.transcripts import write_transcript


class _Result:
    def __init__(self, messages, tokens=(10, 5)):
        self._messages = messages
        self.usage = SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1])

    def all_messages(self):
        return self._messages


def _transcript_files(tmp_path):
    d = tmp_path / "workspace" / "transcripts"
    return sorted(d.glob("*.jsonl")) if d.exists() else []


def test_transcripts_disabled_writes_nothing(tmp_path):
    deps = build_deps(load_config(tmp_path))  # no log_transcripts in settings
    try:
        write_transcript(deps, "demo", result=_Result([]), duration=1.0, ok=True)
    finally:
        close_deps(deps)
    assert _transcript_files(tmp_path) == []


def test_transcripts_write_header_and_parts(tmp_path):
    (tmp_path / "settings.yaml").write_text("log_transcripts: true\n", encoding="utf-8")
    deps = build_deps(load_config(tmp_path))
    messages = [
        ModelRequest(parts=[UserPromptPart(content="hi there")]),
        ModelResponse(parts=[
            TextPart(content="let me check"),
            ToolCallPart(tool_name="read_file", args={"path": "x.txt"}),
        ]),
        ModelRequest(parts=[ToolReturnPart(tool_name="read_file", content="file contents")]),
    ]
    try:
        write_transcript(deps, "demo task", result=_Result(messages), duration=1.234, ok=True)
    finally:
        close_deps(deps)

    files = _transcript_files(tmp_path)
    assert len(files) == 1
    lines = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    header, *parts = lines
    assert header["task"] == "demo task"
    assert header["duration_s"] == 1.23
    assert header["ok"] is True
    assert header["tokens"] == 15
    assert "error" not in header

    roles = [(p["role"], p["part"]) for p in parts]
    assert roles == [
        ("user", "user-prompt"),
        ("assistant", "text"),
        ("assistant", "tool-call"),
        ("tool", "tool-return"),
    ]
    tool_call = parts[2]
    assert tool_call["tool_name"] == "read_file" and tool_call["args"] == {"path": "x.txt"}
    tool_return = parts[3]
    assert tool_return["content"] == "file contents"


def test_transcripts_error_without_result(tmp_path):
    (tmp_path / "settings.yaml").write_text("log_transcripts: true\n", encoding="utf-8")
    deps = build_deps(load_config(tmp_path))
    try:
        write_transcript(deps, "bad task", result=None, duration=0.5, ok=False, error="boom")
    finally:
        close_deps(deps)

    files = _transcript_files(tmp_path)
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1                        # just the header, no messages
    header = json.loads(lines[0])
    assert header["ok"] is False and header["error"] == "boom"
    assert "tokens" not in header


def test_transcripts_redact_secrets(tmp_path):
    (tmp_path / "settings.yaml").write_text("log_transcripts: true\n", encoding="utf-8")
    (tmp_path / ".env").write_text("PROVIDER=openai\nAPI_KEY=sk-supersecretvalue123\n", encoding="utf-8")
    deps = build_deps(load_config(tmp_path))
    assert deps.secrets, "secret should have been collected from .env"
    messages = [
        ModelRequest(parts=[ToolReturnPart(
            tool_name="run_shell", content="key is sk-supersecretvalue123 in the env",
        )]),
    ]
    try:
        write_transcript(deps, "leak test", result=_Result(messages), duration=0.1, ok=True)
    finally:
        close_deps(deps)

    files = _transcript_files(tmp_path)
    content = files[0].read_text(encoding="utf-8")
    assert "sk-supersecretvalue123" not in content
    assert "[secret:API_KEY]" in content


def test_transcripts_prune_keeps_only_newest(tmp_path):
    (tmp_path / "settings.yaml").write_text(
        "log_transcripts: true\ntranscripts_keep: 2\n", encoding="utf-8"
    )
    deps = build_deps(load_config(tmp_path))
    try:
        for i in range(4):
            write_transcript(deps, f"task {i}", result=_Result([]), duration=0.1, ok=True)
    finally:
        close_deps(deps)
    assert len(_transcript_files(tmp_path)) == 2
