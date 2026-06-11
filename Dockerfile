# Lean container image for running a genesis-agent (defaults to --serve).
# Build:  docker build -t genesis-agent .
# Run:    docker run --rm -p 8181:8181 --env-file .env genesis-agent
FROM python:3.12-slim

# uv binary from the official image — no pip bootstrap needed.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
ENV PYTHONUTF8=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# 1) Dependency layer — cached unless pyproject/lock change.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

# 2) Application code + the project itself.
COPY . .
RUN uv sync --frozen --no-dev

EXPOSE 8181
# Headless HTTP service. Bind 0.0.0.0 (NOT the localhost default) so the server
# is reachable through the published port; the host firewall / -p mapping is the
# real boundary. Override for a one-shot task, e.g.:
#   docker run --rm --env-file .env genesis-agent uv run agent "your task"
CMD ["uv", "run", "agent", "--serve", "--host", "0.0.0.0", "--port", "8181"]
