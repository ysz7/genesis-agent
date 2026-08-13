#!/usr/bin/env bash
# Launch this agent. Usage: ./scripts/run.sh "your task"   (no task -> REPL)
set -euo pipefail
cd "$(dirname "$0")/.."          # agent root (parent of scripts/)
export PYTHONUTF8=1

if command -v uv >/dev/null 2>&1; then
  exec uv run agent "$@"
else
  exec python3 -m uv run agent "$@"
fi
