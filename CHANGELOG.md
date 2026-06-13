# Changelog

All notable changes to **genesis-agent** are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/).

If you copied this template, compare this file against upstream to see what
changed since your copy — and skim the **Security** / **Changed** notes before
syncing, since some releases change defaults.

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
