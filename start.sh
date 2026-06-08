#!/usr/bin/env bash
# Double-click (or run ./start.sh) to open the agent REPL.
# Also works with a task or flags:  ./start.sh "your task"  /  ./start.sh --serve
cd "$(dirname "$0")"
export PYTHONUTF8=1

# Find a uv launcher: prefer `uv` on PATH, else `python3 -m uv`.
if command -v uv >/dev/null 2>&1; then
  UV="uv"
elif python3 -m uv --version >/dev/null 2>&1; then
  UV="python3 -m uv"
else
  cat <<'EOF'

  First-time setup needed. Run the installer once:

      ./scripts/install.sh

  (installs uv + dependencies), then run this file again.

EOF
  exit 1
fi

# `uv run` auto-creates .venv and installs deps on first run.
# No args -> interactive start menu; with args -> pass straight through.
if [ $# -eq 0 ]; then
  exec $UV run agent --menu
else
  exec $UV run agent "$@"
fi
