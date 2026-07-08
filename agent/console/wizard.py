"""New-agent wizard — interactive scaffolding of a vertical agent.

Run via ``agent --new`` (what ``new.cmd`` / ``new.sh`` launch on a double-click)
or from the start menu. Collects the agent's name, role, provider, model, and
API key, then creates a sibling folder next to the current agent: a copy of the
frozen engine + scripts, with a generated ``persona.md`` / ``settings.yaml`` /
``.env``. The scaffolding (``scaffold_agent``) is the single, cross-platform
implementation — the shell launchers only kick it off.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import display
from .menu import (
    EMERALD,
    PROVIDERS,
    _clear,
    _DEFAULT_MODELS,  # runtime.config.PROVIDER_DEFAULTS — the one bump point
    _edit_line,
    console,
    select,
)


def _pause() -> None:
    try:
        input("  press Enter to return…")
    except (EOFError, KeyboardInterrupt):
        pass


# Copied verbatim into a new agent. The new-agent wizard launchers live in
# scripts/ and ride along with the scripts/ copytree below; start.cmd/start.sh
# stay in root and are listed here. NOT copied: .env, workspace/, examples/, the
# source agent's own tools, PLAN/CLAUDE/_probe.
_COPY_FILES = [
    "pyproject.toml", "uv.lock", "README.md", ".gitignore", "schedule.example",
    ".env.example",
    "start.cmd", "start.sh",
    "Dockerfile", "docker-compose.yml", ".dockerignore",
]

# Smaller context/output budgets for local models (Ollama), so a scaffolded
# local agent is sane out of the box. Keyed by settings.yaml key → value.
_LOCAL_PROFILE = {
    "history_keep": "15",
    "context_budget": "24000",
    "max_tool_output": "8000",
}


def run_wizard(root: str | None = None) -> int:
    src = Path(root or os.getcwd()).resolve()

    _clear()
    console.print(f"\n  [bold {EMERALD}]New agent[/]   [dim]created next to {src.name}/[/]")
    console.print("  [dim]Enter to confirm each field · Esc to cancel.[/]\n")

    name = _edit_line("  name (folder)= ", "")
    if not name or not name.strip():
        return 0
    name = name.strip()
    dest = src.parent / name
    if dest.exists():
        display.err(f"{dest} already exists — choose another name.")
        _pause()
        return 1

    role = _edit_line("  role / what it does= ", "") or "A capable, concise assistant."

    p = select("Provider", PROVIDERS)
    if p is None:
        return 0
    provider = PROVIDERS[p]

    model = (_edit_line("  model= ", _DEFAULT_MODELS[provider]) or "").strip() or _DEFAULT_MODELS[provider]

    api_key = ""
    if provider != "ollama":
        api_key = (_edit_line("  API key (blank = set later)= ", "") or "").strip()

    try:
        scaffold_agent(src, dest, name, role.strip(), provider, model, api_key)
    except Exception as exc:  # noqa: BLE001
        display.err(f"scaffold failed: {exc}")
        _pause()
        return 1

    _clear()
    display.ok(f"created [bold]{dest}[/]")
    console.print()
    console.print(f"  [dim]cd ../{name}[/]")
    if not api_key and provider != "ollama":
        console.print("  [dim]edit .env  (set API_KEY)[/]")
    console.print("  [dim]edit persona.md, drop tools in tools/, then run start.cmd / ./start.sh[/]")
    console.print()
    _pause()
    return 0


def scaffold_agent(
    src: Path, dest: Path, name: str, role: str, provider: str, model: str, api_key: str
) -> None:
    """Create *dest* as a fresh vertical agent cloned from *src*'s engine."""
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    dest.mkdir(parents=True)
    (dest / "workspace").mkdir()
    (dest / "tools").mkdir()

    shutil.copytree(src / "agent", dest / "agent", ignore=ignore)
    if (src / "scripts").is_dir():
        shutil.copytree(src / "scripts", dest / "scripts", ignore=ignore)
    for fname in _COPY_FILES:
        f = src / fname
        if f.exists():
            shutil.copy2(f, dest / fname)
    example = src / "tools" / "_example.py"
    if example.exists():
        shutil.copy2(example, dest / "tools" / "_example.py")

    (dest / "persona.md").write_text(_persona(name, role), encoding="utf-8")
    (dest / "settings.yaml").write_text(_settings(src, name, provider), encoding="utf-8")
    (dest / ".env").write_text(_env(provider, model, api_key), encoding="utf-8")


def _persona(name: str, role: str) -> str:
    return f"""# Persona — {name}

## Role

{role}

## Rules

- Prefer acting (using a tool) over guessing.
- Use `run_shell` for anything without a dedicated tool — it is the workhorse.
- Be honest about uncertainty and tool failures; report what actually happened.
- Keep answers concise unless asked for detail.
"""


def _settings(src: Path, name: str, provider: str) -> str:
    """The full, fully-commented template settings.yaml with answers filled in.

    Read from the source agent (so the scaffold never drifts from the template's
    guidance), with the name set and — for Ollama — the small-context local
    profile applied so a local agent is sane out of the box.
    """
    template = src / "settings.yaml"
    if not template.exists():  # stripped source — fall back to a minimal file
        return f"name: {name}\nstore: state.json\nretries: 2\n"

    overrides = {"name": name}
    note = ""
    if provider == "ollama":
        overrides.update(_LOCAL_PROFILE)
        note = (
            "# NOTE: Ollama caps context at its default num_ctx (~4k) unless you raise\n"
            "# OLLAMA_CONTEXT_LENGTH or the model's num_ctx. The local-profile values\n"
            "# below assume ~32k — match context_budget to the num_ctx you actually set.\n\n"
        )

    lines = template.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue  # never touch commented guidance
        for key, value in overrides.items():
            if stripped.startswith(f"{key}:"):
                indent = line[: len(line) - len(stripped)]
                lines[i] = f"{indent}{key}: {value}"
                break
    return note + "\n".join(lines) + "\n"


def _env(provider: str, model: str, api_key: str) -> str:
    lines = [f"PROVIDER={provider}", f"MODEL={model}", f"API_KEY={api_key}"]
    if provider == "ollama":
        lines.append("BASE_URL=http://localhost:11434/v1")
    else:
        lines.append("# BASE_URL auto-set for openrouter; leave blank otherwise.")
    return "\n".join(lines) + "\n"
