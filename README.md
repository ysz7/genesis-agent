<div align="center">

<img src="docs/assets/banner.svg" alt="genesis-agent" width="520">

**A lightweight, modular vertical-agent template built on [Pydantic AI](https://ai.pydantic.dev).**

*Copy the folder · edit one file · drop in tools → a specialized agent is ready.*

![CI](https://github.com/ysz7/genesis-agent/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.10+-064e3b?logo=python&logoColor=white)
![Pydantic AI](https://img.shields.io/badge/built%20on-Pydantic%20AI-047857)
![uv](https://img.shields.io/badge/packaged%20with-uv-059669)
![Providers](https://img.shields.io/badge/providers-OpenAI%20·%20Anthropic%20·%20OpenRouter%20·%20Ollama-10b981)
![License](https://img.shields.io/badge/license-MIT-34d399)

</div>

---

You want your own AI agent — a trading assistant, a research bot, a support
automation. Building it from scratch means weeks of plumbing before any real
work: model APIs, tool calling, retries, state, a console, deployment.

**genesis-agent removes that part.** It's a strong, finished foundation for
agents of any complexity: copy the folder, describe your agent's role in
`persona.md`, drop your domain tools into `tools/` — done. Everything generic
is already built and stays frozen: provider wiring (OpenAI · Anthropic ·
OpenRouter · offline Ollama), automatic tool discovery, the agent loop,
cross-run memory, a live console. You write only what makes your agent *yours*.

And unlike heavyweight frameworks, there's no magic to fight: the whole engine
is ~3.8k lines of readable Python on Pydantic AI — small enough to read in an
evening, simple enough to trust in production.

It runs in any environment from day one — interactive terminal, headless HTTP
service, Docker container, or on a schedule via cron / Task Scheduler. A fresh
copy is already a working general-purpose agent with five built-in tools:

<img src="docs/assets/genesis-agent-chat-cli.png" alt="genesis-agent live console: identity and capabilities panels, then a task executed as a reasoning tree with a tokens/time footer">

## Quickstart

Open a terminal in an **empty folder** and paste — this downloads the project,
installs `uv` and all dependencies, and creates `.env`:

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/ysz7/genesis-agent/main/scripts/install.ps1 | iex
```

```bash
# Linux / macOS
curl -LsSf https://raw.githubusercontent.com/ysz7/genesis-agent/main/scripts/install.sh | sh
```

Then set `PROVIDER` / `MODEL` / `API_KEY` in `.env` and launch: **`start.cmd`**
(Windows) / **`./start.sh`** (Linux/macOS).

Manual install (clone first):

```bash
git clone https://github.com/ysz7/genesis-agent.git
cd genesis-agent
powershell -ExecutionPolicy Bypass -File scripts\install.ps1   # Windows
./scripts/install.sh                                           # Linux/macOS
```

- No API key? Set `PROVIDER=ollama`, `MODEL=llama3.1:8b`,
  `BASE_URL=http://localhost:11434/v1` — fully offline.
- Forked the repo? Point the installer at it: edit `$Repo` / `REPO` in
  `scripts/install.*` or set `GENESIS_REPO=...`.

## Features

- **Stands on Pydantic AI** — provider-agnostic models, native tool calling,
  retries, schema-from-type-hints. No hand-rolled transport or JSON schema.
- **Drop-in tools** — any documented, type-hinted function in `tools/*.py` is
  auto-discovered and registered. No wiring.
- **4 providers, switched via `.env`** — OpenAI · Anthropic · OpenRouter ·
  Ollama (offline, no key).
- **Live console** — reasoning tree (reason → tool → result) with a
  `tokens · cost · elapsed` footer.
- **State store** — `get/set/append/all` over JSON or SQLite for cross-run
  memory; **structured output** — return a typed Pydantic model instead of prose.
- **Conversation memory** — the REPL threads history across turns and
  auto-compacts it into a summary when a session outgrows the context budget.
- **Safe by default** — built-in file tools are workspace-sandboxed; a tool
  policy can disable or human-confirm risky tools; the HTTP server binds
  localhost and accepts an optional bearer token.
- **Bounded & tunable** — per-run usage limits (request/token caps) and model
  settings (temperature, `max_tokens`, …) straight from `settings.yaml`.
- **Headless HTTP mode** (`--serve`, zero extra deps) with **SSE streaming**,
  **optional [MCP](https://modelcontextprotocol.io) servers**, **Docker-ready**.
- **Observable** — optional [Logfire](https://logfire.pydantic.dev) tracing, a
  local JSONL run log, and an opt-in `pydantic-evals` harness for your vertical.
- **Scales by copy** — one folder + one process per agent. 50 agents = 50 folders.

## Usage

**`start.cmd`** / **`./start.sh`** opens an arrow-key start menu: Chat ·
Scheduler · Settings · Serve · Quit. The launchers find `uv` and auto-install
deps on first run.

<img src="docs/assets/genesis-agent-welcome-cli.png" alt="genesis-agent start menu" width="300">

Pass a task or flags to skip the menu:

```bash
start.cmd "Summarize the README in three bullets"   # one-shot
start.cmd --serve                                    # HTTP service
```

From a terminal, run `uv` **inside the agent folder** — `.env` / `persona.md` /
`settings.yaml` are loaded from the current directory (use `--root path/to/agent`
from elsewhere):

```bash
uv run agent "Summarize the README in three bullets"   # one-shot
uv run agent                                            # interactive REPL
uv run agent --serve --port 8181                        # HTTP service
```

In the **REPL**, type a task or a command: `/help` · `/tools` · `/clear`
(forget the conversation) · `/reload` (pick up newly approved tools) · `/quit`.

The **HTTP server** binds `127.0.0.1` (localhost only) by default — pass
`--host 0.0.0.0` to accept remote connections (the Docker image does this). Set
`SERVER_TOKEN` in `.env` to require `Authorization: Bearer <token>` on every
endpoint except `/health`.

```bash
# one-shot JSON
curl -X POST localhost:8181/task -H "content-type: application/json" \
     -d '{"task": "what files are in the workspace?"}'

# with a bearer token (when SERVER_TOKEN is set)
curl -X POST localhost:8181/task -H "Authorization: Bearer $SERVER_TOKEN" \
     -H "content-type: application/json" -d '{"task": "hi"}'

# stream the run as Server-Sent Events (text / tool / tool_result / done frames)
curl -N "localhost:8181/task/stream?q=list+the+files+here"
```

Endpoints: `POST /task` · `GET /task?q=...` (browser-friendly) ·
`GET /task/stream?q=...` (SSE) · `GET /health` (open, no auth).

## Make a vertical agent

Run the wizard: **`scripts/new-agent.cmd`** / **`./scripts/new-agent.sh`** (or
*Create a new agent* in the menu). Enter name, role, provider, model, key — it
scaffolds a ready-to-run agent in a sibling folder `../<name>` with a generated
`persona.md` / `settings.yaml` / `.env` and a copy of the engine.

Then refine it:

1. Edit **`persona.md`** — the system prompt.
2. Drop domain tools into **`tools/`** — one documented, type-hinted function
   per tool; take `ctx: RunContext[AgentDeps]` as the first parameter to reach
   the http client / store / settings.
3. Run **`start.cmd`** / `./start.sh`.

A fully filled-in vertical lives in
[`examples/rss_research/`](examples/rss_research/) — drop-in tool,
settings-driven feeds, store-based dedup, structured output.

## Configuration

Non-secret config lives in **`settings.yaml`** (loaded into `deps.settings`);
secrets live in **`.env`**. Every key below ships commented in the template
files with the same notes — this is just the consolidated reference.

**`.env`** (secrets):

| Key            | Purpose |
|----------------|---------|
| `PROVIDER` · `MODEL` · `API_KEY` · `BASE_URL` | model selection (see [Providers](#providers)) |
| `SERVER_TOKEN` | optional `--serve` bearer token; unset = no auth |
| `LOGFIRE_TOKEN` | enables Logfire tracing when `--extra obs` is installed |

**`settings.yaml`** (non-secret):

| Key | Default | What it does |
|-----|---------|--------------|
| `name` | folder name | display name |
| `store` | `state.json` | state file in `workspace/` (`*.json` or `*.db` SQLite) |
| `retries` | `2` | Pydantic AI retries per failed tool/model call |
| `max_tool_output` | `20000` | char cap on a tool's output (`run_shell`, `fetch_url`, HTML cleaner) |
| `history_keep` | `40` | REPL messages kept between turns |
| `context_budget` | `100000` | model's usable context (tokens); compaction triggers at ~60% |
| `compaction` | `enabled: true, keep: 12` | summarize old history past the budget |
| `limits` | `request_limit: 25` | per-run ceilings (`pydantic_ai.usage.UsageLimits`) |
| `model_settings` | — | `temperature`, `max_tokens`, `timeout`, … passed to the model |
| `sandbox` | `true` | confine file tools to `workspace/`; `false` to allow any path |
| `tools` | — | `disable: [...]` (never registered) · `confirm: [...]` (human y/N) |
| `serve_timeout` | `300` | per-task wall-clock seconds for `--serve` → `504` |
| `log_runs` | `false` | append one JSON line per run to `workspace/runs.jsonl` |
| `mcp` | — | external [MCP](#mcp-servers-optional) servers |

The tool policy is the key safety lever: `fetch_url` content is
attacker-controlled (prompt injection), so an unconfirmed `run_shell` is an
injection-to-RCE chain — `confirm: [run_shell]` or `disable: [run_shell]` when
inputs are untrusted. (Headless `--serve` has no human, so a confirm-listed
tool refuses to run rather than executing unattended.)

Built-in `fetch_url` returns HTML as readable text (tags stripped, links
rendered as `text (href)`); pass `raw=True` for the untouched markup.

## Providers

| `PROVIDER`   | `MODEL` example                | API key | Notes |
|--------------|--------------------------------|---------|-------|
| `openai`     | `gpt-4o-mini`                  | ✓       | |
| `anthropic`  | `claude-haiku-4-5`             | ✓       | |
| `openrouter` | `openai/gpt-oss-120b:free`     | ✓       | `BASE_URL` auto-set |
| `ollama`     | `llama3.1:8b`                  | ✗       | offline, no key needed |

Switching is a `.env` edit — no code changes.

### Running on local models (Ollama)

Local models work, with two gotchas:

- **The context trap.** Ollama silently truncates context to its default
  `num_ctx` (~4k) *regardless of what the model supports* — the agent "goes
  dumb" with no error. Raise it (`OLLAMA_CONTEXT_LENGTH=32768`, or a model
  `num_ctx`) **and** set `context_budget` in `settings.yaml` to match, so
  compaction triggers before Ollama starts dropping your prompt.
- **Small-context profile.** On a tight budget, shrink tool output and the
  budget and disable heavy tools:

  ```yaml
  # settings.yaml — for an 8k-context local model
  context_budget: 6000
  max_tool_output: 3000
  tools:
    disable: [run_shell]     # keep the model focused; re-enable as needed
  ```

- **Model choice.** Use a model trained for tool calling — `qwen2.5` 7B+ is a
  reliable floor; expect flaky tool/structured output below ~7B. These hints
  are mirrored in `settings.yaml` comments for first-time users.

## MCP servers (optional)

```bash
uv sync --extra mcp
```

```yaml
# settings.yaml
mcp:
  - name: demo
    command: python
    args: ["examples/mcp_demo/echo_server.py"]   # local stdio server
  - name: docs
    url: https://example.com/mcp                  # remote server
```

Their tools appear to the agent like built-ins (prefixed with `name`). Demo:
[`examples/mcp_demo/`](examples/mcp_demo/). Without an `mcp:` block nothing changes.

## Observability (optional)

Two independent, opt-in layers — the core never imports either, so default
runs are unchanged:

- **Logfire tracing:** `uv sync --extra obs`, then set `LOGFIRE_TOKEN` in
  `.env`. Every model and tool call is traced. Absent the token it degrades
  silently.
- **Local run log:** `log_runs: true` in `settings.yaml` appends one JSON line
  per run (task, duration, tokens, ok/err) to `workspace/runs.jsonl` — greppable
  history, zero external services.

## Evaluating your vertical (optional)

Score your agent against golden tasks with [pydantic-evals](https://ai.pydantic.dev/evals/):

```bash
uv sync --extra evals
uv run python evals/example_eval.py
```

[`evals/example_eval.py`](evals/example_eval.py) is a copyable template — a tiny
`Dataset` of cases scored by a plain (no-second-model) `Contains` check, run
against the live agent. Swap in your own cases and evaluators. The core never
imports `pydantic_evals`.

## Self-improvement (optional)

Off by default. Enable it and the agent gets tools to extend itself, all
sandboxed to `workspace/`:

```yaml
# settings.yaml
self_improvement:
  enabled: true
```

- **Skills** (the primary path) — `write_skill` / `read_skill` save reusable
  procedures as markdown under `workspace/skills/`. Not code, so no approval; a
  one-line index is injected into the system prompt and pulled in full on demand.
- **Memory** — `remember(lesson)` appends to `workspace/memory/lessons.jsonl`; a
  digest of recent lessons rides in the system prompt next session.
- **Tools** — `write_tool` authors a Python tool under `workspace/tools/`. It
  runs **only** after passing checks (syntax → banned-import scan → load + tool
  contract) **and** a human approval. Approvals are three-way (once · always ·
  deny); an "always" grant persists in `workspace/approvals.json` keyed by a hash
  of the code, so editing the file re-triggers approval. In the REPL, `/reload`
  (or automatic reload after approval) makes a new tool callable in the same
  session. Headless `--serve` has no human, so activation is denied unless
  `approvals.headless_allow_granted` honors a prior grant.

To **improve** an existing skill or tool, the agent reads it (`read_skill` /
`read_tool`), revises, and saves under the same name — `write_skill` /
`write_tool` overwrite, and a changed tool re-runs validation and approval.

The human approval — not the validation — is the security boundary; generated
files carry a provenance header (when, prompting task, model) for auditability.

## Docker

```bash
cp .env.example .env
docker compose up --build      # serves POST /task on :8181
```

`workspace/` is mounted as a volume, so state persists. One-shot:
`docker run --rm --env-file .env genesis-agent uv run agent "your task"`.

Inside the container the server binds `0.0.0.0` (the image's `CMD` passes
`--host 0.0.0.0`) so the published port is reachable — the host `-p` mapping is
the real boundary. Set `SERVER_TOKEN` in `.env` to require bearer auth when the
port is exposed beyond localhost.

## Scheduling

**In-app** — the *Scheduler* menu item runs recurring tasks in a live feed.
Jobs persist in the state store but fire only while the scheduler is open.

**External (24/7)** — drive one-shot runs with cron / systemd / Task Scheduler
via `scripts/run.sh` / `scripts/run.ps1` (not `start.cmd` — it ends with
`pause`). Templates: [`schedule.example`](schedule.example).

```bash
# cron — every hour
0 * * * * /path/to/agent/scripts/run.sh "Run the hourly briefing" >> /path/to/agent/workspace/cron.log 2>&1
```

```powershell
# Windows Task Scheduler — daily at 9am
$root    = "C:\path\to\agent"
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$root\scripts\run.ps1`" `"Run the hourly briefing`""
$trigger = New-ScheduledTaskTrigger -Daily -At 9am
Register-ScheduledTask -TaskName "genesis-agent" -Action $action -Trigger $trigger
```

## Project structure

```
genesis-agent/
├── agent/                  the frozen engine (never edited per vertical)
│   ├── __main__.py         entrypoint: menu · one-shot · REPL · --serve
│   ├── __init__.py         public API: `from agent import AgentDeps, parse_rss`
│   ├── runtime/            config · context (AgentDeps) · store · runlog · approvals
│   ├── engine/             model · registry · factory · mcp · compaction · runner
│   ├── tools/              builtins · toolkit (http/cache/rss/html) · selfimprove
│   ├── console/            display (rich tree · spinner · stats) · menu
│   └── server/             stdlib HTTP: POST /task · SSE /task/stream · live monitor
├── persona.md              the vertical's system prompt          ← yours
├── settings.yaml           non-secret config (feeds, mcp, …)     ← yours
├── .env                    secrets (provider, model, key)        ← yours
├── tools/                  drop-in custom tools (auto-discovered) ← yours
├── workspace/              runtime sandbox (created on first run):
│   ├── files/              task outputs (write_file default)
│   ├── tools/ · skills/    agent-authored, approved tools + skills (opt-in)
│   └── memory/             reflection lessons
├── examples/               filled-in verticals to copy from
├── evals/                  copyable pydantic-evals harness (opt-in)
├── scripts/                install · run · fleet · new-agent helpers
├── start.cmd / start.sh    double-click launchers (start menu)
└── Dockerfile · docker-compose.yml
```

## Changelog

Notable changes per release are in [CHANGELOG.md](CHANGELOG.md). If you copied
this template, compare it against upstream to see what changed since your copy —
some releases change defaults (e.g. the server now binds localhost), so skim the
**Security** / **Changed** notes before syncing.

## License

MIT — see [LICENSE](LICENSE).
