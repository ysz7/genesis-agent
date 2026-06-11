#!/usr/bin/env bash
# fleet.sh — manage many genesis-agents living under a directory.
# Each agent is a folder containing a persona.md. No shared registry.
#
#   ./scripts/fleet.sh list             # list agent folders
#   ./scripts/fleet.sh run "task"       # run a one-shot task in every agent
#   ./scripts/fleet.sh serve            # start each agent as a background HTTP server
#   ./scripts/fleet.sh stop             # stop all background servers
#
# By default scans the directory that contains this agent's folder (its
# siblings). Set FLEET_ROOT to scan elsewhere.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"      # .../<agent>/scripts
agent_root="$(dirname "$here")"            # .../<agent>
root="${FLEET_ROOT:-$(dirname "$agent_root")}"
cmd="${1:-list}"

agents() {
  find "$root" -maxdepth 2 -name persona.md \
    -not -path '*/examples/*' -not -path '*/agent/*' 2>/dev/null \
    | xargs -n1 dirname 2>/dev/null | sort -u
}

case "$cmd" in
  list)
    agents
    ;;
  run)
    task="${2:?usage: ./scripts/fleet.sh run \"task\"}"
    for a in $(agents); do
      echo "── $a ──────────────────────────────"
      ( cd "$a" && uv run agent "$task" ) || echo "  (failed)"
    done
    ;;
  serve)
    port=8181
    for a in $(agents); do
      echo "starting $a on :$port"
      ( cd "$a" && nohup uv run agent --serve --port "$port" \
          > workspace/serve.log 2>&1 & )
      port=$((port + 1))
    done
    ;;
  stop)
    pkill -f "uv run agent --serve" 2>/dev/null || echo "no servers running"
    ;;
  *)
    echo "usage: ./scripts/fleet.sh [list | run \"task\" | serve | stop]" >&2
    exit 1
    ;;
esac
