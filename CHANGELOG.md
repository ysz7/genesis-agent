# Changelog

All notable changes to **genesis-agent** are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/).

If you copied this template, compare this file against upstream to see what
changed since your copy — and skim the **Security** / **Changed** notes before
syncing, since some releases change defaults.

## [1.2.1] — 2026-06-29

### Added
- **Agent-managed scheduling** — the agent can now create recurring tasks from a
  chat (`schedule_task`, `list_scheduled`, `cancel_scheduled`, `edit_scheduled`),
  e.g. "summarize HN every 2 hours". Jobs persist in the store and fire **in the
  background** while a gateway bot or the HTTP server (`--serve`) is up; each
  result is **delivered to all channels** (every allowlisted Telegram/WhatsApp
  user, the CLI feed if open, the server log). A shared owner-lock runs each due
  job exactly once across processes. On by default (`scheduler:` in
  `settings.yaml`: `enabled`, `tick`, `max_jobs`). The menu's Scheduler now shares
  the same `scheduled_jobs` store and schema. (Previously the scheduler was
  menu-only and fired only while that menu loop was open; the agent had no way to
  schedule, list, or cancel tasks.)
- **`web_search` built-in tool** — the agent can now search the web for current
  information (news, prices, weather, docs, "today") via DuckDuckGo (no API key),
  then `fetch_url` a result to read it in full. Previously the agent could only
  GET a single known URL, so questions needing a search (e.g. live weather) often
  failed. Re-exported as `from agent import web_search`; swap in a search API
  (Tavily/Brave) in your own `tools/` file for heavy use.

Messaging gateways (Phase 22) — chat with the agent from Telegram & WhatsApp.
"One brain, many channels": a fourth thin entrypoint next to the CLI and the HTTP
server, built into the core (no extra to install, no heavy SDK — pure `httpx`).

### Added
- **Telegram gateway** — run it locally with long-polling (no public URL needed)
  from the menu's new **Gateways** screen or `agent --gateway telegram`. Each
  channel runs as **its own process** (PID-tracked), so a bot started from the
  menu keeps running after you leave it and works in parallel with the CLI.
- **WhatsApp gateway** — Meta Cloud API, webhook-only: run `agent --serve` and
  point Meta's webhook at `POST /webhook/whatsapp` (with the `GET` verification
  handshake handled).
- **Webhooks on `--serve`** — every enabled gateway also mounts at
  `POST /webhook/<name>`, verified against `WEBHOOK_SECRET`, for production/Docker
  deployments. The same channel that long-polls locally is driven by webhooks on
  a server.
- **Per-user memory** — each platform user maps to its own persistent thread
  (`<gateway>:<user_id>`), so conversations are isolated and survive a restart.
  Gateways use the concurrent SQLite/WAL store (a JSON store is flagged).
- **Deny-all access control** — empty allowlist means nobody; an unknown user is
  refused with their id echoed back. The Telegram owner manages access in chat
  with `/allow`, `/deny`, `/allowlist` (and `/whoami`), or you manage it from the
  menu. The owner id always passes (bootstrap).
- **Inbound media** — Telegram photos/documents are downloaded and attached:
  images/PDF as multimodal parts (vision), text documents inlined into the prompt.
- **Formatted replies** — the model's markdown is rendered to Telegram's HTML
  subset by default (**bold**, `code`, lists, links — no more raw `**`), with a
  plain-text fallback if Telegram rejects it (`parse_mode: plain` to force plain;
  WhatsApp replies are flattened to plain text).
- **Inline-button approvals (Telegram)** — confirm-gated tools route to the chat
  as Allow / Always / Deny buttons instead of being refused headlessly; channels
  without buttons fall back to refusal.
- **Per-user daily quota** (`max_messages_per_day`) and Telegram flood-wait (429
  `retry_after`) backoff, to bound token spend and respect rate limits.
- **Live monitor** — a gateway started from the menu opens in its own console
  window with a banner (channel · token · owner · allowlist · store · model), a
  live feed (each message in → reply with tokens/elapsed; blocked/errors), and a
  closing stats panel. Headless runs log plainly to `workspace/gateways/<name>.log`
  (always written, so the menu's *View log* works either way).
- Configure everything under `gateways:` in `settings.yaml` (off by default;
  dormant until a token is set in `.env`).

## [1.0.1] — 2026-06-28

CLI / REPL polish — a `prompt_toolkit`-powered prompt, file & document
attachments, Markdown answers, and fixes to paste / emoji handling.

### Added
- **REPL line editing via `prompt_toolkit`** (new core dependency, used only by
  the CLI — the server stays rich/pt-free). The interactive prompt now supports
  **correct multi-line paste** (cross-platform, including the Windows console) —
  a multi-line paste collapses to a `[Pasted text +N lines]` placeholder so it
  doesn't flood the screen, expanded back on submit (Backspace/Delete remove the
  whole placeholder atomically) — plus **command history**
  (↑/↓, persisted to `workspace/.repl_history`) and proper line editing. The rich
  output (reasoning tree, panels, stats) is unchanged — only the input line is
  read differently. Ctrl+C cancels the current line; Ctrl+D or `/quit` exits.
- **Attach files & documents in chat** — `/attach <path>` (repeatable) sends a
  file with your next message: images/PDF go to the model as multimodal parts
  (vision models), and text documents (code, `.md`, `.csv`, `.json`, …) are read
  and inlined into the prompt so they work on any model. **Dragging a file into
  the terminal** collapses its path to a `[file: name]` chip (instead of a long
  raw path), removed atomically with one Backspace — same as the paste placeholder.

### Fixed
- The CLI now renders the agent's final answer as **Markdown** (bold, lists,
  `code`, headings) instead of printing raw syntax like `**bold**`. Falls back to
  plain text if rendering fails; set `render_markdown: false` for verbatim output.
  (Server/JSON output is unchanged — raw text.)
- **Pasting a long / multi-line message into the REPL** no longer truncates it or
  leaks the remaining lines into later prompts (the earlier symptom where pasted
  tails appeared as later prompts and a single Ctrl+C couldn't catch up). The
  `prompt_toolkit` reader takes the whole paste as one input.
- **Pasting/typing emoji in the REPL no longer crashes** with a "surrogates not
  allowed" UTF-8 error. On Windows, non-BMP characters arrive as lone UTF-16
  surrogate halves; they're now recombined before the text is used or written to
  history.
- **REPL output spacing** — a blank line now separates your input (and the
  `attached:` line) from the reasoning tree, so it no longer looks crammed.

## [1.0.0] — 2026-06-27

### Added
- **Persistent conversation threads** (`threads.enabled`, Phase 18). A
  conversation can survive a restart: it's serialized into the existing state
  store under a `session_id`. In the REPL, `agent --session work` resumes a named
  thread, and `/threads` · `/resume <id>` · `/new` manage them; each turn is saved
  back. The HTTP server gains an optional `POST {"task": ..., "session": "<id>"}`
  that loads and saves that conversation — **without a session the server stays
  stateless**, as before. Off by default; threads are capped to `history_keep`
  and a corrupt/missing blob degrades to a fresh conversation (never a crash).
- **Model fallback** (`model_fallbacks`, Phase 20). Set a list of backup model
  ids and a transient primary failure (HTTP error, rate-limit, outage) retries
  the next one transparently — same provider/key, wrapped in Pydantic AI's
  `FallbackModel`. For availability, not bad output; shown in the startup banner.
  Unset = single model, unchanged.
- **Input / output guardrails** (`guardrails`, Phase 21). An opt-in, regex-based
  content layer on top of the tool policy: `input.block` refuses a matching task
  (never sent to the model), `input.redact` masks spans before sending;
  `output.block` makes a matching answer retry then fail cleanly, `output.redact`
  masks spans in the answer. Ships the seam, not a policy library — no heavy
  dependency. Off by default.
- **Semantic long-term memory** (`memory.semantic`, Phase 19). With it on,
  `remember` stores an embedding alongside each lesson and recall ranks them by
  **relevance** to the current task (cosine similarity) instead of recency —
  lightweight, no vector database (embeddings via the provider's `/embeddings`
  endpoint; pure-Python similarity). Any embedding failure degrades to recency;
  off by default, behaviour unchanged.

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
