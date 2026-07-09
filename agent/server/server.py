"""Optional stdlib HTTP service — zero extra dependencies, no rich.

``agent --serve`` mounts the same Agent headless over Python's built-in
``http.server``::

    POST /task              {"task": "..."}            → {"output": ...}
    GET  /task?q=...        browser-friendly           → {"output": ...}
    GET  /task/stream?q=... text/event-stream          → incremental SSE frames
    GET  /deliveries        pending scheduled results  → {"deliveries": [...]}
    POST /webhook/<gw>      messaging-gateway inbound  → {"ok": true}
    GET  /health            open (no auth)             → {"status": "ok"}

The Agent, model, tools, and deps are identical to the CLI path — only the
rendering differs. This module deliberately never imports ``display``; it shares
the agent event-walk with the CLI through ``engine.runner.iter_events`` instead.

**One event loop, entered once.** A single background loop thread runs for the
whole serve lifetime, and ``async with agent`` is entered once on it — so MCP
servers start once, not per request. ``ThreadingHTTPServer`` still gives a thread
per request; each handler submits its coroutine to the shared loop with
``run_coroutine_threadsafe`` (the Agent is reentrant-safe in Pydantic AI, and
``deps`` is lock-guarded). A per-task ``serve_timeout`` (settings, default 300s)
caps runaway model calls with a ``504``.

Each request is **stateless by default**: unlike the REPL (which threads a running
conversation via ``message_history``), every HTTP task runs independently with no
memory of prior requests. A caller that wants continuity sends the context in the
task itself — or, when ``threads.enabled`` (Phase 18), passes
``{"task": ..., "session": "<id>"}`` to load and save a persisted conversation
across requests (the default, no ``session``, stays stateless).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx

from ..runtime.config import Config
from ..runtime.context import build_deps, close_deps
from ..runtime import threads
from ..runtime.runlog import append_run
from ..runtime.transcripts import write_transcript
from ..runtime.attachments import build_user_prompt, max_mb_from, prompt_text, vision_hint
from ..engine.factory import build_agent
from ..engine import guardrails
from ..engine.runner import Done, Reason, Think, ToolCall, ToolResult, iter_events
from ..gateways import discover_gateways, gateway_enabled
from ..gateways.base import store_guard
from ..runtime import scheduler

MAX_BODY = 1_048_576  # 1 MB — reject larger POST bodies with 413 before reading.


def _make_httpd(config: Config, host: str, port: int, monitor):
    """Build the agent + deps + a shared event loop, and a configured server.

    Returns ``(httpd, deps)``. The loop thread and the entered agent context are
    stashed on the httpd for :func:`_teardown` to unwind on shutdown.
    """
    agent = build_agent(config)
    deps = build_deps(config)
    token = config.server_token
    serve_timeout = float(config.settings.get("serve_timeout", 300))

    # One loop for the whole serve lifetime; enter the agent context once on it
    # (starts MCP servers, if any). Handlers submit coroutines to this loop.
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True, name="agent-loop").start()
    asyncio.run_coroutine_threadsafe(agent.__aenter__(), loop).result()

    # Messaging webhooks (Phase 22g): for every enabled gateway, mount
    # ``POST /webhook/<name>``. The same channel that long-polls locally is driven
    # by inbound webhook updates here, sharing this agent + deps + loop. Inbound is
    # verified against WEBHOOK_SECRET and processed fire-and-forget (a webhook must
    # return 200 promptly; the agent run continues on the loop).
    webhooks: dict = {}
    for name, cls in discover_gateways(config).items():
        if gateway_enabled(config.settings, name):
            gw = cls(config, deps)
            gw._pipeline = gw.make_pipeline(agent)
            webhooks[name] = gw
    webhook_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0)) if webhooks else None
    webhook_secret = os.getenv("WEBHOOK_SECRET") or None
    for gw in webhooks.values():
        gw.on_mounted(webhook_client, loop)   # e.g. bind approval-buttons bridge
    if webhooks:
        problem = store_guard(deps.store)
        if problem:
            print(f"note: gateways share this store — {problem}")

    # Background scheduler (Phase 23): run due jobs (under the shared owner-lock)
    # and drain server-addressed deliveries (logged) while serving.
    sched_future = None
    if (config.settings.get("scheduler") or {}).get("enabled"):
        tick = float((config.settings.get("scheduler") or {}).get("tick", 30))
        owner = scheduler.default_owner_id()

        async def _scheduler_loop():
            log = logging.getLogger("agent.scheduler")
            while True:
                try:
                    await scheduler.run_due_jobs(
                        deps.store, agent, deps,
                        owner_id=owner, ttl=max(60.0, tick * 2),
                        usage_limits=config.usage_limits,
                    )
                    # Log drain uses its own consumption key so GET /deliveries
                    # (which consumes "server") still sees each record once.
                    for rec in scheduler.pending_for(deps.store, "server", consumer="server-log"):
                        log.info("scheduled delivery [%s]: %s",
                                 rec.get("job_id"), str(rec.get("text"))[:200])
                        scheduler.mark_delivered(deps.store, rec["id"], "server-log")
                    # Webhook-only channels (WhatsApp) have no loop of their own —
                    # deliver their scheduled results from here (Phase 25a).
                    for gw in webhooks.values():
                        await gw.deliver_pending(webhook_client)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    log.warning("scheduler loop error: %s", exc)
                await asyncio.sleep(tick)

        sched_future = asyncio.run_coroutine_threadsafe(_scheduler_loop(), loop)

    def _submit(coro, timeout: float | None = serve_timeout):
        """Run *coro* on the shared loop, bounded by *timeout*. Blocks the caller.

        The timeout lives inside the coroutine (``wait_for``) so it cancels the
        agent run rather than orphaning it; ``TimeoutError`` propagates out.
        """
        async def _bounded():
            if timeout is None:
                return await coro
            return await asyncio.wait_for(coro, timeout=timeout)

        return asyncio.run_coroutine_threadsafe(_bounded(), loop).result()

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
            if parsed.path.startswith("/webhook/"):
                self._handle_webhook_get(parsed)
            elif parsed.path == "/health":
                self._send(200, {"status": "ok", "agent": config.agent_name})
            elif parsed.path == "/task":
                if not self._authorized():
                    return
                # Browser-friendly: open  http://localhost:PORT/task?q=your+task
                task = self._query_task(parsed)
                if task:
                    self._handle_task(task)
            elif parsed.path == "/task/stream":
                if not self._authorized():
                    return
                task = self._query_task(parsed)
                if task:
                    self._handle_stream(task)
            elif parsed.path == "/deliveries":
                if not self._authorized():
                    return
                self._handle_deliveries()
            else:
                self._send(404, {"error": "not found"})

        def _handle_deliveries(self) -> None:
            """Return pending scheduled-task results for external pollers (Phase 26a).

            Each record is returned once (consumed under the ``server`` key —
            independent of the ticker's log drain, which uses ``server-log``).
            """
            from ..runtime import scheduler as _sched

            records = _sched.pending_for(deps.store, "server")
            out = [
                {"id": r.get("id"), "job_id": r.get("job_id"),
                 "text": r.get("text"), "ts": r.get("ts")}
                for r in records
            ]
            for r in records:
                _sched.mark_delivered(deps.store, r["id"], "server")
            self._send(200, {"deliveries": out})

        def _query_task(self, parsed) -> str | None:
            params = parse_qs(parsed.query)
            task = (params.get("q") or params.get("task") or [""])[0]
            if not task.strip():
                self._send(400, {"error": f"GET {parsed.path} needs ?q=<task>"})
                return None
            allowed, task = guardrails.check_input(config.settings, task)
            if not allowed:
                self._send(400, {"error": task})
                return None
            return task

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            self._detailed = False
            if self.path.startswith("/webhook/"):
                self._handle_webhook()
                return
            if self.path != "/task":
                self._send(404, {"error": "not found"})
                return
            if not self._authorized():
                return
            raw = self._read_body()
            if raw is None:
                return                       # _read_body already sent the error
            try:
                data = json.loads(raw or b"{}")
                task = data["task"]
            except (json.JSONDecodeError, KeyError):
                self._send(400, {"error": "expected JSON body with a 'task' field"})
                return
            # Input guardrails (Phase 21): refuse/redact before building the prompt.
            allowed, task = guardrails.check_input(config.settings, task)
            if not allowed:
                self._send(400, {"error": task})
                return
            # Optional multimodal input — URLs only (a remote local path would be
            # an arbitrary-file-read hole).
            prompt = build_user_prompt(
                task, data.get("images") or [], allow_local=False,
                max_mb=max_mb_from(config.settings),
            )
            # Optional persistent thread (Phase 18): a caller may pass a session
            # id to carry a conversation across requests. Stateless without it.
            self._handle_task(prompt, session=data.get("session"))

        def _send_text(self, code: int, text: str) -> None:
            """Send a plain-text response (webhook handshakes want raw text)."""
            body = (text or "").encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("content-type", "text/plain")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionError, BrokenPipeError, OSError):
                pass

        def _handle_webhook_get(self, parsed) -> None:
            """GET ``/webhook/<gateway>`` — a channel's verification handshake (Meta)."""
            name = parsed.path[len("/webhook/"):].strip("/")
            gw = webhooks.get(name)
            if gw is None:
                self._send(404, {"error": f"no gateway mounted at /webhook/{name}"})
                return
            result = gw.webhook_verify(parse_qs(parsed.query))
            if result is None:
                self._send(404, {"error": f"{name} has no GET handshake"})
                return
            code, text = result
            self._send_text(code, text)

        def _handle_webhook(self) -> None:
            """Accept an inbound platform update for ``/webhook/<gateway>`` (Phase 22g).

            Verifies the shared secret, parses the update, and hands it to the
            gateway on the shared loop *without waiting* — a webhook must answer
            200 quickly while the agent run continues in the background.
            """
            name = self.path[len("/webhook/"):].strip("/")
            # Always drain the request body first — replying without reading it
            # resets the connection (the client sees a read error, not the status).
            raw = self._read_body()
            if raw is None:
                return                          # _read_body already sent the error
            gw = webhooks.get(name)
            if gw is None:
                self._send(404, {"error": f"no gateway mounted at /webhook/{name}"})
                return
            # Channel-specific auth: Telegram checks the secret-token header,
            # WhatsApp verifies Meta's X-Hub-Signature-256 over the raw body.
            if not gw.webhook_authorized(self.headers, raw, webhook_secret):
                self._send(401, {"error": "webhook authentication failed"})
                return
            try:
                update = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._send(400, {"error": "invalid JSON update"})
                return
            # Fire-and-forget on the shared loop; handle_update catches its own errors.
            asyncio.run_coroutine_threadsafe(gw.handle_update(webhook_client, update), loop)
            if monitor:
                try:
                    monitor.on_access("POST", self.path, 200, self.client_address[0])
                except Exception:  # noqa: BLE001
                    pass
            self._send(200, {"ok": True})

        def _read_body(self) -> bytes | None:
            """Read the request body, enforcing the size limit. None on error sent."""
            raw_len = self.headers.get("content-length")
            if raw_len is None:
                self._send(411, {"error": "content-length required"})
                return None
            try:
                length = int(raw_len)
            except ValueError:
                self._send(400, {"error": "invalid content-length"})
                return None
            if length > MAX_BODY:
                self._send(413, {"error": f"request body exceeds {MAX_BODY} bytes"})
                return None
            return self.rfile.read(length) if length else b"{}"

        def _handle_task(self, task, session=None) -> None:
            self._detailed = True            # use the detailed monitor feed
            deps.extra.pop("plan", None)     # per-request todo scratchpad (Phase 13)
            task_text = prompt_text(task)    # text form for monitor/log (not bytes)
            start = time.monotonic()
            if monitor:
                monitor.on_request(task_text, self.client_address[0])
            # Persistent thread (Phase 18): load prior history when a session id is
            # given AND threads are enabled; otherwise the request is stateless.
            use_thread = bool(session) and threads.enabled(config.settings)
            history = threads.load_thread(deps.store, session) if use_thread else []
            # Run the agent and send the response as SEPARATE steps: a failure to
            # deliver (client closed the tab) must not mislabel a task that ran
            # fine, nor trigger a second send onto a dead socket.
            try:
                result = _submit(agent.run(
                    task, deps=deps, message_history=history or None,
                    usage_limits=config.usage_limits,
                ))
            except (asyncio.TimeoutError, TimeoutError):
                elapsed = time.monotonic() - start
                if monitor:
                    monitor.on_result(False, 0, elapsed)
                append_run(deps, task_text, elapsed, 0, ok=False, error="timeout")
                write_transcript(deps, task_text, duration=elapsed, ok=False, error="timeout")
                self._send(504, {"error": f"task exceeded {serve_timeout:.0f}s timeout"})
                return
            except Exception as exc:  # noqa: BLE001 - the task itself failed
                elapsed = time.monotonic() - start
                if monitor:
                    monitor.on_result(False, 0, elapsed)
                append_run(deps, task_text, elapsed, 0, ok=False, error=str(exc))
                write_transcript(deps, task_text, duration=elapsed, ok=False, error=str(exc))
                self._send(500, {"error": f"{exc}{vision_hint(exc)}"})
                return
            elapsed = time.monotonic() - start
            if monitor:
                monitor.on_result(True, _tokens(result), elapsed)
            append_run(deps, task_text, elapsed, _tokens(result), ok=True)
            write_transcript(deps, task_text, result=result, duration=elapsed, ok=True)
            if use_thread:                   # persist the updated conversation
                keep = int(config.settings.get("history_keep", 40))
                threads.save_thread(deps.store, session, result.all_messages(), keep=keep)
            self._send(200, {"output": _jsonable(result.output)})

        def _handle_stream(self, task: str) -> None:
            """Stream the run as Server-Sent Events: text / tool / tool_result / done."""
            self._detailed = True
            deps.extra.pop("plan", None)     # per-request todo scratchpad (Phase 13)
            start = time.monotonic()
            if monitor:
                monitor.on_request(task, self.client_address[0])
            try:
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                self.send_header("connection", "close")
                self.end_headers()
            except (ConnectionError, BrokenPipeError, OSError):
                return

            # The producer runs on the shared loop and posts frames to a queue;
            # this handler thread drains the queue and writes them out.
            q: queue.Queue = queue.Queue()

            async def _produce():
                async def _drive():
                    async for ev in iter_events(agent, task, deps):
                        q.put(("frame", _sse_for(ev)))
                        if isinstance(ev, Done):
                            q.put(("done", ev.result))
                try:
                    await asyncio.wait_for(_drive(), timeout=serve_timeout)
                except (asyncio.TimeoutError, TimeoutError):
                    q.put(("frame", _sse("error", {"error": "timeout"})))
                except Exception as exc:  # noqa: BLE001
                    q.put(("frame", _sse("error", {"error": str(exc)})))
                finally:
                    q.put(("end", None))

            fut = asyncio.run_coroutine_threadsafe(_produce(), loop)
            result = None
            ok = True
            try:
                while True:
                    kind, payload = q.get()
                    if kind == "end":
                        break
                    if kind == "done":
                        result = payload
                        continue
                    try:
                        self.wfile.write(payload.encode("utf-8"))
                        self.wfile.flush()
                    except (ConnectionError, BrokenPipeError, OSError):
                        fut.cancel()        # client gone — stop the run
                        ok = False
                        return
            finally:
                if not fut.done():
                    fut.cancel()
                elapsed = time.monotonic() - start
                tokens = _tokens(result) if result is not None else 0
                ok = ok and result is not None
                if monitor:
                    monitor.on_result(ok, tokens, elapsed)
                append_run(deps, task, elapsed, tokens, ok=ok)
                write_transcript(deps, task, result=result, duration=elapsed, ok=ok)

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd._agent = agent  # type: ignore[attr-defined]
    httpd._loop = loop    # type: ignore[attr-defined]
    httpd._webhook_client = webhook_client  # type: ignore[attr-defined]
    httpd._sched_future = sched_future  # type: ignore[attr-defined]
    return httpd, deps


def _teardown(httpd, deps) -> None:
    """Exit the agent context, stop the loop thread, release deps."""
    loop = getattr(httpd, "_loop", None)
    agent = getattr(httpd, "_agent", None)
    webhook_client = getattr(httpd, "_webhook_client", None)
    sched_future = getattr(httpd, "_sched_future", None)
    httpd.server_close()
    if sched_future is not None:
        sched_future.cancel()
    if loop is not None and agent is not None:
        if webhook_client is not None:
            try:
                asyncio.run_coroutine_threadsafe(webhook_client.aclose(), loop).result(timeout=5)
            except Exception:  # noqa: BLE001 - best-effort shutdown
                pass
        try:
            asyncio.run_coroutine_threadsafe(
                agent.__aexit__(None, None, None), loop
            ).result(timeout=10)
        except Exception:  # noqa: BLE001 - best-effort shutdown
            pass
        loop.call_soon_threadsafe(loop.stop)
    close_deps(deps)


def serve(config: Config, port: int = 8181, monitor=None, host: str = "127.0.0.1") -> int:
    """Build the agent once and serve until interrupted (blocking).

    Binds *host* (default ``127.0.0.1`` — localhost only). Pass ``0.0.0.0`` to
    accept connections from other machines, e.g. inside a container reached
    through a published port (the Dockerfile does exactly this).

    *monitor* (optional) receives ``on_start`` / ``on_request`` / ``on_result`` /
    ``on_access`` callbacks for a live request feed. It's the only rendering hook;
    this module never imports rich, so headless and Docker runs stay clean.
    """
    httpd, deps = _make_httpd(config, host, port, monitor)
    if monitor:
        monitor.on_start()
    else:
        auth = "  (bearer auth on)" if config.server_token else ""
        print(
            f"genesis-agent '{config.agent_name}' serving on "
            f"http://{host}:{port}  (POST /task · GET /task/stream){auth}"
        )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        _teardown(httpd, deps)
    return 0


def start_background(config: Config, port: int = 8181, monitor=None, host: str = "127.0.0.1"):
    """Serve in a daemon thread; returns (httpd, deps) for the caller to drive.

    Used by the interactive serve console and tests: the HTTP server runs in the
    background while the foreground drives it. Stop with ``stop_background`` (or
    ``httpd.shutdown()`` then :func:`_teardown`).
    """
    httpd, deps = _make_httpd(config, host, port, monitor)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, deps


def stop_background(httpd, deps) -> None:
    """Stop a :func:`start_background` server and unwind its loop + deps."""
    httpd.shutdown()
    _teardown(httpd, deps)


# ── SSE framing ──────────────────────────────────────────────────────────────

def _sse(event: str, data: dict) -> str:
    """One Server-Sent Events frame."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_for(ev) -> str:
    """Map a runner event to its SSE frame."""
    if isinstance(ev, Think):
        return _sse("thinking", {"text": ev.text})
    if isinstance(ev, Reason):
        return _sse("text", {"text": ev.text})
    if isinstance(ev, ToolCall):
        return _sse("tool", {"name": ev.name, "args": _jsonable(ev.args)})
    if isinstance(ev, ToolResult):
        return _sse("tool_result", {"name": ev.name, "result": str(ev.content)})
    if isinstance(ev, Done):
        return _sse("done", {"output": _jsonable(ev.result.output)})
    return _sse("text", {"text": str(ev)})


# ── helpers ──────────────────────────────────────────────────────────────────

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
