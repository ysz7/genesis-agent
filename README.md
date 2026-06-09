<div align="center">

# 🤖 micro-agent

**A lightweight, modular vertical-agent template built on [Pydantic AI](https://ai.pydantic.dev).**

*Copy the folder · edit one file · drop in tools → a specialized agent is ready.*

![Python](https://img.shields.io/badge/python-3.10+-3776AB?logo=python&logoColor=white)
![Pydantic AI](https://img.shields.io/badge/built%20on-Pydantic%20AI-e92063)
![uv](https://img.shields.io/badge/packaged%20with-uv-de5fe9)
![Providers](https://img.shields.io/badge/providers-OpenAI%20·%20Anthropic%20·%20OpenRouter%20·%20Ollama-2ea44f)
![License](https://img.shields.io/badge/license-MIT-blue)

</div>

---

micro-agent is a **finished, runnable template** — not a skeleton you complete
each time. There are exactly two layers, and you only ever touch the second:

- 🧊 **Frozen engine** (`agent/`) — model wiring, tool discovery, the agent loop,
  the console, the HTTP server. Built once, never edited per agent.
- ✍️ **Per-agent** — `persona.md`, `tools/*.py`, `settings.yaml`, `.env`. That's
  all you write for a new vertical.

A fresh copy is already a working general-purpose agent with five built-in tools —
**before any customization**.

```text
  › write hello.txt with "hi" then list the workspace
  ┌ write_file  ·  hello.txt · 2 chars  ·  Wrote 2 chars to workspace/hello.txt
  ├ list_dir    ·  .  ·  2 items
  └ done
  ↳  1,164 tok (1,084→80)  ·  $0.0003  ·  2.3s
```

## ✨ Features

- **Stands on Pydantic AI** — provider-agnostic models, native tool calling,
  retries, and schema-from-type-hints. No hand-rolled LLM transport or JSON schema.
- **Drop-in tools** — any documented, type-hinted function in `tools/*.py` is
  auto-discovered and registered. No wiring.
- **4 providers, switch via `.env`** — OpenAI · Anthropic · OpenRouter · Ollama
  (offline, no key). One line changes the brain.
- **Majestic console** — a live reasoning tree (reason → tool → result), a
  spinner, and a `tokens · cost · elapsed` footer, built on `rich`.
- **Built-in state store** — `get/set/append/all` over JSON or SQLite for
  cross-run memory (dedup sets, history, counters).
- **Structured output** — return a typed Pydantic model instead of prose.
- **Headless HTTP mode** — `--serve` exposes `POST /task` with zero extra deps.
- **Scales by copy** — one folder + one process per agent. 50 agents = 50 folders.
- **Optional MCP** — plug external [MCP](https://modelcontextprotocol.io) tool
  servers in from config. Lean by default.
- **Docker-ready** — slim `uv` image + compose for deployment.

## 🚀 Install

### One-liner — into an empty folder

Make a folder, open a terminal in it, and paste. This **downloads the project,
installs `uv` + all dependencies, and creates `.env`** — nothing pre-installed
needed (`uv` is a standalone binary that brings its own Python):

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/ysz7/micro-agent/main/scripts/install.ps1 | iex
```

```bash
# Linux / macOS
curl -LsSf https://raw.githubusercontent.com/ysz7/micro-agent/main/scripts/install.sh | sh
```

Then **edit `.env`** (set `PROVIDER` / `MODEL` / `API_KEY`) and launch:
double-click **`start.cmd`** (Windows) / run **`./start.sh`** (Linux/macOS).
Every run after that is just `start`.

### Manual (clone first)

```bash
git clone https://github.com/ysz7/micro-agent.git
cd micro-agent
powershell -ExecutionPolicy Bypass -File scripts\install.ps1   # Windows
./scripts/install.sh                                           # Linux/macOS
```

> 💡 No API key? Use **Ollama**: set `PROVIDER=ollama`, `MODEL=llama3.1:8b`,
> `BASE_URL=http://localhost:11434/v1` and run fully offline.

> ⚙️ Forked it? Point the installer at your own repo by editing `$Repo` / `REPO`
> at the top of `scripts/install.ps1` / `scripts/install.sh` (or set
> `MICROAGENT_REPO=...`). It works the same whether run remotely (empty folder →
> it clones) or locally (inside the repo → it just sets up).

## ▶️ Usage

**Easiest — the launchers.** Double-click **`start.cmd`** (Windows) or run
**`./start.sh`** (Linux/macOS) to open an arrow-key **start menu**: Chat ·
Scheduler (recurring tasks while the agent is open) · Settings (edit provider /
model / API key in `.env`) · Serve (HTTP with a live request monitor) · Quit. The
launchers `cd` into the folder, find `uv` (with a clear hint if it's missing), and
auto-install deps on first run. Pass a task or flags to skip the menu:

```bash
start.cmd "Summarize the README in three bullets"   # one-shot
start.cmd --serve                                    # HTTP service
```

**From a terminal — `uv` directly** (run from inside the agent folder):

```bash
uv run agent "Summarize the README in three bullets"   # one-shot
uv run agent                                            # interactive REPL
```

> ℹ️ `uv run agent` must run **from inside the agent folder** — `uv` locates the
> project there, and the agent loads `.env` / `persona.md` / `settings.yaml` from
> the current directory. (Or pass `--root path/to/agent` from elsewhere.)

Run as an HTTP service:

```bash
uv run agent --serve --port 8181
curl -X POST localhost:8181/task -H "content-type: application/json" \
     -d '{"task": "what files are in the workspace?"}'
```

## 🧩 Make a vertical agent

```bash
./scripts/new.ps1 trading-bot     # Windows  → creates ../trading-bot
./scripts/new.sh  trading-bot     # Linux/macOS
```

Then, in the new folder:

1. Edit **`persona.md`** — role, domain knowledge, rules.
2. Drop domain tools into **`tools/`** — one function per tool (docstring + type
   hints; add `ctx: RunContext[AgentDeps]` to reach the http client / store / settings).
3. Set **`.env`** (provider, model, key) and **`settings.yaml`** (feeds, symbols…).
4. Double-click **`start.cmd`** (or `./start.sh`) — ready.

The engine in `agent/` is never touched. See
[`examples/rss_research/`](examples/rss_research/) for a fully filled-in vertical
(drop-in tool · settings-driven feeds · store-based dedup · structured output).

## 🔌 Providers

| `PROVIDER`   | `MODEL` example                | Key | Notes |
|--------------|--------------------------------|-----|-------|
| `openai`     | `gpt-4o-mini`                  | ✅  | |
| `anthropic`  | `claude-haiku-4-5`             | ✅  | |
| `openrouter` | `openai/gpt-oss-120b:free`     | ✅  | `BASE_URL` auto-set |
| `ollama`     | `llama3.1:8b`                  | ❌  | offline, OpenAI-compat path |

Adding a provider is just editing `.env` — no code changes.

## 🛠️ MCP servers (optional)

Plug external MCP tool servers in without writing code:

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

Their tools appear to the agent like built-ins (prefixed with `name`). A working
demo lives in [`examples/mcp_demo/`](examples/mcp_demo/). With no `mcp:` block the
agent runs exactly as before.

## 🐳 Docker

```bash
cp .env.example .env
docker compose up --build      # serves POST /task on :8181
```

`workspace/` is mounted as a volume so state persists. One-shot run:
`docker run --rm --env-file .env micro-agent uv run agent "your task"`.

## ⏰ Scheduling

Two ways, for two needs:

**In-app (while the agent is open)** — the **Scheduler** menu item. Add recurring
tasks (e.g. "summarize new feeds" every 5 minutes), hit *Run scheduler (live)*,
and watch them fire in a live feed; Ctrl+C stops. Jobs are saved in the state
store, so they're remembered — but they only run while the scheduler is open.

**External (even when closed)** — the agent runs one-shot from a task string, so
cron / systemd / Task Scheduler drives runs that fire 24/7 without a terminal. Use
`scripts/run.sh` / `scripts/run.ps1` (not `start.cmd`, which ends with `pause`).
See [`schedule.example`](schedule.example) for templates.

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
Register-ScheduledTask -TaskName "micro-agent" -Action $action -Trigger $trigger
```

## 📁 Project structure

```
micro-agent/
├── agent/                  🧊 the frozen engine (never edited per vertical)
│   ├── __main__.py         entrypoint: menu · one-shot · REPL · --serve
│   ├── __init__.py         public API: `from agent import AgentDeps, parse_rss`
│   ├── runtime/            config · context (AgentDeps) · store (JSON|SQLite)
│   ├── engine/             model · registry · factory · mcp (builds the Agent)
│   ├── tools/              builtins (5 tools) · toolkit (http/cache/rss helpers)
│   ├── console/            display (rich tree · spinner · stats) · menu
│   └── server/             stdlib HTTP POST /task + live monitor
├── persona.md              ✍️ the vertical's system prompt
├── settings.yaml           ✍️ non-secret config (feeds, symbols, thresholds, mcp)
├── .env                    ✍️ secrets (provider, model, key)
├── tools/                  ✍️ drop-in custom tools (auto-discovered)
├── examples/               filled-in verticals to copy from
├── scripts/                install · run · new · fleet helpers (ps1 + sh)
├── start.cmd / start.sh    double-click launchers (start menu)
└── Dockerfile · docker-compose.yml
```

## 📜 License

MIT — see [LICENSE](LICENSE).
