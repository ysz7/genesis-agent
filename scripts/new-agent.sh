#!/usr/bin/env bash
# Open the wizard that scaffolds a NEW agent in a sibling folder.
# Double-click (or run ./scripts/new-agent.sh).
cd "$(dirname "$0")/.."
export PYTHONUTF8=1

if command -v uv >/dev/null 2>&1; then
  UV="uv"
elif python3 -m uv --version >/dev/null 2>&1; then
  UV="python3 -m uv"
else
  echo
  echo "  First-time setup needed. Run the installer once:"
  echo "      ./scripts/install.sh"
  echo
  exit 1
fi

exec $UV run agent --new
