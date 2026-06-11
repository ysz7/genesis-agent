"""Optional stdlib HTTP service — zero extra dependencies, no rich.

``agent --serve`` mounts a ``POST /task`` endpoint over Python's built-in
``http.server`` that runs the same Agent headless::

    curl -X POST localhost:8181/task \
         -H 'content-type: application/json' \
         -d '{"task": "what files are here?"}'

The Agent, model, tools, and deps are all identical to the CLI path — only the
rendering differs. This module deliberately never imports ``display``.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..runtime.config import Config
from ..runtime.context import build_deps, close_deps
from ..engine.factory import build_agent


def _make_httpd(config: Config, host: str, port: int, monitor):
    """Build the agent + deps and a configured HTTP server. Returns (httpd, deps)."""
    agent = build_agent(config)
    deps = build_deps(config)
    token = config.server_token

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):  # quiet default logging
            pass

        def _authorized(self) -> bool:
            """True if no token is configured or the bearer token matches.

            ``/health`` is always open (probes/orchestrators need it); every
            other endpoint requires ``Authorization: Bearer <SERVER_TOKEN>`` when
            a token is set. On failure this sends 401 and returns False.
            """
            if not token:
                return True
            header = self.headers.get("authorization", "")
            expected = f"Bearer {token}"
            if len(header) == len(expected) and hmac.compare_digest(header, expected):
                return True
            self._send(401, {"error": "unauthorized"})
            return False

        def handle(self) -> None:
            # A client that disconnects mid-request (e.g. closes the browser tab)
            # raises a connection error somewhere in the stdlib machinery — that's
            # normal, not a server fault, so swallow it instead of dumping a trace.
            try:
                super().handle()
            except (ConnectionError, BrokenPipeError, OSError):
                pass

        def _send(self, code: int, payload: dict) -> bool:
            """Send a JSON response. Returns False if the client had gone away."""
            body = json.dumps(payload).encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionError, BrokenPipeError, OSError):
                return False        # client disconnected before we finished
            # Log every request (any client) — except the detailed /task flow,
            # which already prints its own →/← lines.
            if monitor and not getattr(self, "_detailed", False):
                try:
                    monitor.on_access(self.command, self.path, code, self.client_address[0])
                except Exception:  # noqa: BLE001
                    pass
            return True

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            self._detailed = False
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                self._send(200, {"status": "ok", "agent": config.agent_name})
            elif parsed.path == "/task":
                if not self._authorized():
                    return
                # Browser-friendly: open  http://localhost:PORT/task?q=your+task
                params = parse_qs(parsed.query)
                task = (params.get("q") or params.get("task") or [""])[0]
                if task.strip():
                    self._handle_task(task)
                else:
                    self._send(400, {"error": "GET /task needs ?q=<task>"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            self._detailed = False
            if self.path != "/task":
                self._send(404, {"error": "not found"})
                return
            if not self._authorized():
                return
            length = int(self.headers.get("content-length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw or b"{}")
                task = data["task"]
            except (json.JSONDecodeError, KeyError):
                self._send(400, {"error": "expected JSON body with a 'task' field"})
                return
            self._handle_task(task)

        def _handle_task(self, task: str) -> None:
            self._detailed = True            # use the detailed monitor feed
            start = time.monotonic()
            if monitor:
                monitor.on_request(task, self.client_address[0])
            # Run the agent and send the response as SEPARATE steps: a failure to
            # deliver (client closed the tab) must not mislabel a task that ran
            # fine, nor trigger a second send onto a dead socket.
            try:
                result = asyncio.run(_run(task))
            except Exception as exc:  # noqa: BLE001 - the task itself failed
                if monitor:
                    monitor.on_result(False, 0, time.monotonic() - start)
                self._send(500, {"error": str(exc)})
                return
            if monitor:
                monitor.on_result(True, _tokens(result), time.monotonic() - start)
            self._send(200, {"output": _jsonable(result.output)})

    async def _run(task: str):
        # `async with agent` starts/stops any MCP servers (no-op without them).
        async with agent:
            return await agent.run(task, deps=deps)

    return ThreadingHTTPServer((host, port), Handler), deps


def serve(config: Config, port: int = 8181, monitor=None, host: str = "127.0.0.1") -> int:
    """Build the agent once and serve ``POST /task`` until interrupted (blocking).

    Binds *host* (default ``127.0.0.1`` — localhost only). Pass ``0.0.0.0`` to
    accept connections from other machines, e.g. inside a container reached
    through a published port (the Dockerfile does exactly this).

    *monitor* (optional) receives ``on_start`` / ``on_request`` / ``on_result``
    callbacks for a live request feed. It's the only rendering hook; this module
    never imports rich, so headless and Docker runs stay dependency-clean.
    """
    httpd, deps = _make_httpd(config, host, port, monitor)
    if monitor:
        monitor.on_start()
    else:
        auth = "  (bearer auth on)" if config.server_token else ""
        print(
            f"genesis-agent '{config.agent_name}' serving on "
            f"http://{host}:{port}  (POST /task){auth}"
        )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()
        close_deps(deps)
    return 0


def start_background(config: Config, port: int = 8181, monitor=None, host: str = "127.0.0.1"):
    """Serve in a daemon thread; returns (httpd, deps) for the caller to drive.

    Used by the interactive serve console: the HTTP server runs in the background
    while the foreground reads commands. Stop with ``httpd.shutdown()`` then
    ``close_deps(deps)``.
    """
    import threading

    httpd, deps = _make_httpd(config, host, port, monitor)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, deps


def _jsonable(output: object) -> object:
    """Pydantic models → dict; everything else passes through."""
    if hasattr(output, "model_dump"):
        return output.model_dump()
    return output


def _tokens(result) -> int:
    """Total tokens for a run (input + output), best-effort."""
    try:
        usage = result.usage
        usage = usage if hasattr(usage, "input_tokens") else usage()
        return (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0
