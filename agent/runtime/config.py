"""Configuration loader — the ONE place configuration is read.

Loads three sources from the agent folder root:

- ``.env``          — secrets (PROVIDER · MODEL · API_KEY · BASE_URL)
- ``settings.yaml`` — non-secret vertical config (feeds, symbols, thresholds)
- ``persona.md``    — the system prompt for this vertical

Nothing else in the codebase reads these files directly; they take a ``Config``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass
class Config:
    """The fully resolved configuration for one agent instance."""

    root: Path
    provider: str
    model: str
    api_key: str | None
    base_url: str | None
    persona: str
    settings: dict = field(default_factory=dict)
    workspace: Path = field(default_factory=lambda: Path("workspace"))
    # Optional bearer token for the HTTP server (secret → .env, not settings).
    server_token: str | None = None
    # Run-time guardrails from settings.yaml `limits:` — a
    # ``pydantic_ai.usage.UsageLimits`` (or None). Passed at every run site, not
    # to the Agent constructor (limits are a per-run argument).
    usage_limits: Any = None
    # settings.yaml `model_settings:` — a plain dict (ModelSettings TypedDict)
    # passed to ``Agent(model_settings=...)``: temperature, max_tokens, timeout…
    model_settings: dict | None = None

    @property
    def agent_name(self) -> str:
        return str(self.settings.get("name", self.root.name))


_DEFAULT_PERSONA = (
    "You are a capable, concise general-purpose agent. "
    "Use the available tools to inspect files, run shell commands, and fetch "
    "URLs when they help you complete the task. Think step by step, take "
    "actions, and report a clear final answer."
)


def load_config(root: str | os.PathLike | None = None) -> Config:
    """Load ``.env`` + ``settings.yaml`` + ``persona.md`` from *root*.

    *root* defaults to the current working directory (each agent runs from its
    own folder). Missing files fall back to sensible defaults so a bare copy
    still runs.
    """
    root_path = Path(root or os.getcwd()).resolve()

    # 1. secrets
    env_path = root_path / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    provider = (os.getenv("PROVIDER") or "openai").strip().lower()
    model = (os.getenv("MODEL") or _default_model(provider)).strip()
    api_key = os.getenv("API_KEY") or _provider_key(provider)
    base_url = os.getenv("BASE_URL") or _default_base_url(provider)
    server_token = os.getenv("SERVER_TOKEN") or None

    # 2. vertical config
    settings: dict = {}
    settings_path = root_path / "settings.yaml"
    if settings_path.exists():
        loaded = yaml.safe_load(settings_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            settings = loaded

    # 3. persona / system prompt
    persona_path = root_path / "persona.md"
    persona = (
        persona_path.read_text(encoding="utf-8").strip()
        if persona_path.exists()
        else _DEFAULT_PERSONA
    )

    workspace = root_path / str(settings.get("workspace", "workspace"))
    workspace.mkdir(parents=True, exist_ok=True)

    model_settings = settings.get("model_settings")
    if not isinstance(model_settings, dict):
        model_settings = None

    return Config(
        root=root_path,
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        persona=persona,
        settings=settings,
        workspace=workspace,
        server_token=server_token,
        usage_limits=_build_usage_limits(settings),
        model_settings=model_settings,
    )


def _build_usage_limits(settings: dict) -> Any:
    """Build a ``UsageLimits`` from the settings ``limits:`` block, or None.

    Only the keys present are passed, so new ``UsageLimits`` fields can be used
    from settings without touching this function (forward-compatible). Imported
    lazily to keep ``config`` free of heavy imports at module load.
    """
    limits = settings.get("limits")
    if not isinstance(limits, dict) or not limits:
        return None
    from pydantic_ai.usage import UsageLimits

    known = (
        "request_limit",
        "total_tokens_limit",
        "input_tokens_limit",
        "output_tokens_limit",
        "tool_calls_limit",
    )
    kwargs = {k: limits[k] for k in known if k in limits}
    return UsageLimits(**kwargs) if kwargs else None


def _default_model(provider: str) -> str:
    return {
        "openai": "gpt-4o-mini",
        "anthropic": "claude-haiku-4-5",
        "openrouter": "openai/gpt-4o-mini",
        "ollama": "qwen2.5",
    }.get(provider, "gpt-4o-mini")


def _provider_key(provider: str) -> str | None:
    """Fall back to conventional per-provider env vars if API_KEY is unset."""
    return {
        "openai": os.getenv("OPENAI_API_KEY"),
        "anthropic": os.getenv("ANTHROPIC_API_KEY"),
        "openrouter": os.getenv("OPENROUTER_API_KEY"),
        "ollama": "ollama",  # any non-empty value; ollama ignores it
    }.get(provider)


def _default_base_url(provider: str) -> str | None:
    return {
        "openrouter": "https://openrouter.ai/api/v1",
        "ollama": "http://localhost:11434/v1",
    }.get(provider)
