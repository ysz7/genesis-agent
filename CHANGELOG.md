# Changelog

All notable changes to **genesis-agent** are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/).

If you copied this template, compare this file against upstream to see what
changed since your copy — and skim the **Security** / **Changed** notes before
syncing, since some releases change defaults.

## [Unreleased]

### Added
- **Persistent conversation threads** (`threads.enabled`, Phase 18). A
  conversation can survive a restart: it's serialized into the existing state
  store under a `session_id`. In the REPL, `agent --session work` resumes a named
  thread, and `/threads` · `/resume <id>` · `/new` manage them; each turn is saved
  back. The HTTP server gains an optional `POST {"task": ..., "session": "<id>"}`
  that loads and saves that conversation — **without a session the server stays
  stateless**, as before. Off by default; threads are capped to `history_keep`
  and a corrupt/missing blob degrades to a fresh conversation (never a crash).

## [0.9.0] — 2026-06-27

### Added
- **Named, specialized subagents** (`delegate_to`, Phase 17). Beyond the
  anonymous `delegate`, the agent can keep a roster of named specialists — each
  a `workspace/agents/<name>.md` file with its own persona and tool allowance —
  and pick one by description with `delegate_to(name, task)`. Three ways a
  subagent is created, all the same file: the agent authors one with
  `write_agent` (markdown only — creating a new one is free, like `write_skill`;
  *updating* an existing one asks for human approval), you ask it to in chat, or
  you write the file by hand. A new read-only **Subagents** item in
  the start menu lists the roster. A subagent's `tools.allow` / `deny` can only
  narrow the parent's policy; sub-agents never get `write_tool` / `write_agent`;
  the existing depth guard and usage-folding apply unchanged. A subagent may set
  an optional `model:` to run on a different model id on the same provider (e.g.
  a cheaper model for a simple specialist).

### Changed
- **`planning`, `subagents`, and `self_improvement` now default to `enabled:
  true`** in the template `settings.yaml` (set each `enabled: false` to opt out).
  Self-authored *tools* still require human approval, so the safety boundary is
  unchanged. `subagents.allow_authoring` (default `true`) gates the `write_agent`
  authoring tool; set it `false` for a fixed, human-curated roster.
- README documents Planning, Subagents/delegation (including named subagents and
  the three creation paths), and the previously-undocumented `planning` /
  `subagents` / `self_improvement` settings keys.

## [0.8.0] — 2026-06-15

Onboarding and polish — a guided first run, a cleaner console, a refreshed site.

### Added
- **Guided first-run setup.** A fresh install no longer requires hand-editing
  `.env`: the first `start` launch walks you through provider → model → key and
  writes it for you, then opens the menu. Already-configured agents go straight
  to the menu. (Ollama needs no key and skips that step.)

### Changed
- Installers now finish with "Agent ready — run `./start.sh`" instead of
  "edit `.env`", since the first launch configures the agent.
- README and landing page present **two install paths**: one command (fastest)
  or step-by-step clone; the one-command paste also launches straight into setup.
- Refreshed the landing page (`docs/`): new copy and design, kept the emerald
  accent.

### Fixed
- The interactive menu no longer leaves stale frames stacked when navigating
  (clearing now also wipes the scrollback) — fixes the duplicated-screen glitch
  in VS Code / Windows Terminal.

## [0.7.0] — 2026-06-14

Frontier capability baseline — planning, delegation, multimodal, caching.
All new capabilities are opt-in (off by default), so existing agents are
unchanged.

### Added
- **Explicit planning / todo scratchpad** (`planning.enabled`): an `update_plan`
  tool that keeps a short checklist the model maintains across a multi-step task
  — shown in the system prompt each turn and rendered live in the console
  (○ pending → ▸ in_progress → ✓ done).
- **Subagents / delegation** (`subagents.enabled`): a `delegate(task)` tool that
  runs a fresh sub-agent on an isolated subtask (clean context) and folds the
  answer back. Safe by default — depth guard (`max_depth`, no fork bombs),
  restricted toolset (no `write_tool`), and the sub-agent's token cost charged to
  the parent's usage budget.
- **Multimodal input**: attach images/PDFs to a run — one-shot `--image
  PATH_OR_URL`, drag a file into the REPL, or `POST /task` with
  `{"images": ["https://..."]}` (server accepts URLs only). Needs a
  vision-capable model; `attachments.max_mb` caps size.
- **Prompt caching** (`prompt_caching`): reuse the provider's prompt cache —
  Anthropic caches the static tool definitions; the stats footer shows
  "N cached" tokens when a run reads from cache. OpenAI/OpenRouter cache
  automatically; other providers no-op.

### Changed
- Hardened model-error handling: a one-shot run now catches generic
  model/provider errors with a clean message instead of a stack trace, and
  image-related failures suggest switching to a vision-capable model (CLI + the
  server's error payload).

## [0.6.0] — 2026-06-14

Update awareness, a settings.yaml that teaches itself, and a smarter scaffold.

### Added
- **"Check for updates"** menu item — compares your local version (from
  `pyproject.toml`) against the newest semver tag on GitHub and links to the
  changelog / project page. Read-only: it never auto-replaces the engine.
  Override the repo it checks with the `GENESIS_REPO` env var.
- `read_tool` — read back an agent-authored tool's source (provenance header
  stripped) so it can be revised and re-submitted (self-improvement; opt-in).

### Changed
- `settings.yaml` rewritten as a fully-commented, sectioned reference: every key
  states what it does, its default, and recommended values per setup (cloud vs
  small local model) — configurable from the file alone, no source diving.
- `agent --new` now scaffolds that same fully-commented `settings.yaml` (not a
  stripped one) with provider-aware defaults — choosing Ollama presets a
  small-context profile and adds a `num_ctx` note — and copies `.env.example`.

## [0.5.0] — 2026-06-13

Agent self-improvement — **off by default** (`self_improvement.enabled`).

### Added
- `write_skill` / `read_skill` — reusable markdown procedures under
  `workspace/skills/`; a one-line index is injected into the system prompt and
  the full text is pulled on demand.
- `remember(lesson)` — append-only `workspace/memory/lessons.jsonl`; a digest of
  the last `memory_recall` lessons rides in the system prompt next session.
- `write_tool` — the agent authors Python tools under `workspace/tools/`, gated
  by syntax check → banned-import scan → load/tool-contract eval-gate → human
  approval. Generated files carry a provenance header (when · task · model).
- Approval system (`workspace/approvals.json`): three-way **once · always ·
  deny**; "always" grants persist by content hash (editing the code re-prompts).
  Headless honors grants only when `approvals.headless_allow_granted` is set.
- REPL `/reload`, plus automatic reload after a tool is approved, so a new tool
  is callable in the same session.

### Changed
- Workspace layout: `read_file` / `write_file` / `list_dir` now default to
  `workspace/files/` (task outputs), keeping self-authored `tools/`, `skills/`,
  and `memory/` separate. The sandbox boundary remains the whole `workspace/`
  (reach siblings with `../tools/...`).

## [0.4.0] — 2026-06-13

Production-shaped server, leaner tools, and a test/eval harness.

### Added
- **SSE streaming**: `GET /task/stream?q=...` emits `text` / `tool` /
  `tool_result` / `done` frames (shared event-walk with the CLI tree).
- `fetch_url` returns HTML as readable text (links as `text (href)`); `raw=True`
  for untouched markup.
- `max_tool_output` cap (chars) for `run_shell` / `fetch_url` / the HTML cleaner.
- Registry guards: duplicate tool names de-duped (human/builtin wins);
  parameters without type annotations are skipped with a warning.
- Opt-in eval harness (`uv sync --extra evals`, `evals/example_eval.py`).
- Consolidated **Configuration**, **Running on local models**, **Observability**,
  and **Evaluating your vertical** sections in the README.

### Changed
- Server runs one persistent event loop (MCP servers start once per serve, not
  per request); per-task `serve_timeout` → `504`; request body over 1 MB → `413`
  (missing/invalid `Content-Length` → `411`/`400`).

## [0.3.0] — 2026-06-11

Run controls, conversation memory, and observability.

### Added
- `limits:` (per-run `pydantic_ai.usage.UsageLimits`, default `request_limit: 25`)
  and `model_settings:` (`temperature`, `max_tokens`, …), echoed in the banner.
- REPL conversation memory across turns + `/clear`, bounded by `history_keep`.
- Auto-compaction: long history is summarized past `context_budget` instead of
  truncated (`compaction:`), preserving early facts.
- Logging via `agent.*` loggers (rich in the CLI, plain when headless); opt-in
  Logfire tracing (`uv sync --extra obs` + `LOGFIRE_TOKEN`); optional local run
  log (`log_runs` → `workspace/runs.jsonl`).

## [0.2.0] — 2026-06-11

Security hardening. **Changes default behavior — review before upgrading.**

### Security
- **The HTTP server now binds `127.0.0.1` by default** (was `0.0.0.0`). Pass
  `--host 0.0.0.0` to expose it; the Docker image does this so the published
  port stays reachable.
- **The filesystem sandbox is enforced**: `read_file` / `write_file` /
  `list_dir` refuse paths outside `workspace/`. Set `sandbox: false` to opt out.
- Optional `SERVER_TOKEN` bearer auth on every endpoint except `/health`.
- Tool policy `tools: {disable: [...], confirm: [...]}` — disable tools entirely
  or require human approval before a call (refused when headless).

### Changed
- Pinned `pydantic-ai-slim[openai,anthropic]>=1.0,<2`.

## [0.1.0] — 2026-06-11

Initial template.

### Added
- Base agent on Pydantic AI: 5 built-in tools (`read_file`, `write_file`,
  `list_dir`, `run_shell`, `fetch_url`), auto-discovered `tools/*.py`.
- Four providers switched via `.env` (OpenAI · Anthropic · OpenRouter · Ollama).
- Live rich console (reasoning tree + token/cost/elapsed footer), interactive
  start menu, one-shot, REPL, and headless `--serve` HTTP mode.
- JSON/SQLite state store, structured output, optional MCP servers, Docker,
  in-app + external scheduling, and a `new-agent` scaffolding wizard.
