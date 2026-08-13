#!/usr/bin/env bash
# genesis-agent setup / bootstrap (Linux/macOS).
#
#   Bootstrap into an empty folder (downloads the repo, then sets it up):
#     curl -LsSf https://raw.githubusercontent.com/ysz7/genesis-agent/main/scripts/install.sh | sh
#
#   Local (already inside the cloned repo):
#     ./scripts/install.sh
#
# Set the repo below (or override with GENESIS_REPO=...).
set -euo pipefail

REPO="${GENESIS_REPO:-https://github.com/ysz7/genesis-agent}"

echo
echo "=== genesis-agent setup ==="

# --- Locate or download the project -----------------------------------------
root=""
self="${BASH_SOURCE:-$0}"
if [ -f "$(dirname "$self")/../pyproject.toml" ]; then
  root="$(cd "$(dirname "$self")/.." && pwd)"      # run from scripts/
elif [ -f pyproject.toml ]; then
  root="$(pwd)"                                    # run from repo root
fi

if [ -z "$root" ]; then
  # Bootstrap mode: fetch the repo into the current (empty) folder.
  echo "Downloading from $REPO ..."
  if command -v git >/dev/null 2>&1; then
    git clone --depth 1 "$REPO" .
  else
    curl -LsSf "$REPO/archive/refs/heads/main.tar.gz" | tar xz --strip-components=1
  fi
  root="$(pwd)"
fi
cd "$root"

# --- 1) uv (standalone binary; brings its own Python) -----------------------
if command -v uv >/dev/null 2>&1; then
  echo "uv already installed ($(uv --version))"
else
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  echo "uv installed ($(uv --version))"
fi

# --- 2) Dependencies --------------------------------------------------------
echo "Installing dependencies..."
uv sync

# --- 3) Secrets file --------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from template."
else
  echo ".env already exists -- left untouched."
fi

echo
echo "=== Agent ready ==="
echo "  To begin, run:   ./start.sh"
echo "  The first launch walks you through provider, model, and key."
echo
