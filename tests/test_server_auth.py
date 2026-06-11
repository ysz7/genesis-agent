"""Phase 2c: the HTTP server's bearer-auth boundary.

Exercises only the auth gate (no model call): /health stays open, and /task is
401 without the token when one is configured. The happy path / timeouts get full
coverage with a model override in the Phase 8 server tests.
"""

import socket

import httpx

from agent.runtime.config import load_config
from agent.runtime.context import close_deps
from agent.server import start_background


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_health_open_but_task_needs_token(tmp_path):
    config = load_config(tmp_path)
    config.server_token = "s3cret"  # simulate SERVER_TOKEN being set
    port = _free_port()
    httpd, deps = start_background(config, port=port)
    base = f"http://127.0.0.1:{port}"
    try:
        # /health is always reachable (probes/orchestrators rely on it).
        assert httpx.get(f"{base}/health", timeout=5).status_code == 200
        # /task without the token is rejected before any work happens.
        assert httpx.get(f"{base}/task?q=hi", timeout=5).status_code == 401
        # Wrong token is also rejected.
        r = httpx.get(
            f"{base}/task?q=hi",
            headers={"Authorization": "Bearer wrong"},
            timeout=5,
        )
        assert r.status_code == 401
    finally:
        httpd.shutdown()
        httpd.server_close()
        close_deps(deps)
