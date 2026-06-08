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
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config
from .context import build_deps, close_deps
from .factory import build_agent


def serve(config: Config, port: int = 8181, monitor=None) -> int:
    """Build the agent once and serve ``POST /task`` until interrupted.

    *monitor* (optional) receives ``on_start`` / ``on_request`` / ``on_result``
    callbacks for a live request feed. It's the only rendering hook; this module
    never imports rich, so headless and Docker runs stay dependency-clean.
    """
    agent = build_agent(config)
    deps = build_deps(config)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):  # quiet default logging
            pass

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            if self.path == "/health":
                self._send(200, {"status": "ok", "agent": config.agent_name})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            if self.path != "/task":
                self._send(404, {"error": "not found"})
                return
            length = int(self.headers.get("content-length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw or b"{}")
                task = data["task"]
            except (json.JSONDecodeError, KeyError):
                self._send(400, {"error": "expected JSON body with a 'task' field"})
                return
            start = time.monotonic()
            if monitor:
                monitor.on_request(task, self.client_address[0])
            try:
                result = asyncio.run(_run(task))
                if monitor:
                    monitor.on_result(True, _tokens(result), time.monotonic() - start)
                self._send(200, {"output": _jsonable(result.output)})
            except Exception as exc:  # noqa: BLE001 - report to the client
                if monitor:
                    monitor.on_result(False, 0, time.monotonic() - start)
                self._send(500, {"error": str(exc)})

    async def _run(task: str):
        # `async with agent` starts/stops any MCP servers (no-op without them).
        async with agent:
            return await agent.run(task, deps=deps)

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    if monitor:
        monitor.on_start()
    else:
        print(f"micro-agent '{config.agent_name}' serving on http://0.0.0.0:{port}  (POST /task)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()
        close_deps(deps)
    return 0


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
