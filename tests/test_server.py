"""Phase 6 server rework: concurrency, timeout, body limits, SSE.

The server builds its own agent, so we inject a controllable model by
monkeypatching ``factory.build_model``. All offline — no network, no real model.
"""

import asyncio
import socket
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.messages import ModelResponse, TextPart

from agent.engine import factory
from agent.runtime.config import load_config
from agent.server import start_background, stop_background


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(monkeypatch, tmp_path, model, settings_yaml=""):
    (tmp_path / "settings.yaml").write_text(settings_yaml, encoding="utf-8")
    monkeypatch.setattr(factory, "build_model", lambda config: model)
    config = load_config(tmp_path)
    port = _free_port()
    httpd, deps = start_background(config, port=port)
    return httpd, deps, f"http://127.0.0.1:{port}"


def _text_model(text: str = "hello") -> FunctionModel:
    def fn(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content=text)])
    return FunctionModel(fn)


def test_happy_path_post(monkeypatch, tmp_path):
    httpd, deps, base = _serve(monkeypatch, tmp_path, _text_model("hi there"))
    try:
        r = httpx.post(f"{base}/task", json={"task": "go"}, timeout=10)
        assert r.status_code == 200
        assert r.json()["output"] == "hi there"
    finally:
        stop_background(httpd, deps)


def test_concurrent_requests_share_one_loop(monkeypatch, tmp_path):
    # Measure actual overlap rather than wall time (robust under CPU load): each
    # call records the peak number of simultaneously-active runs. On the shared
    # loop both runs reach the sleep before either finishes → peak 2.
    active = {"n": 0, "max": 0}
    lock = threading.Lock()

    async def slow(messages, info: AgentInfo) -> ModelResponse:
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        await asyncio.sleep(0.3)
        with lock:
            active["n"] -= 1
        return ModelResponse(parts=[TextPart(content="done")])

    httpd, deps, base = _serve(monkeypatch, tmp_path, FunctionModel(slow))
    try:
        def hit(_):
            return httpx.post(f"{base}/task", json={"task": "go"}, timeout=10).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(hit, range(2)))

        assert codes == [200, 200]
        assert active["max"] == 2, "requests did not run concurrently on the loop"
    finally:
        stop_background(httpd, deps)


def test_slow_task_times_out_504(monkeypatch, tmp_path):
    async def hang(messages, info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(5)
        return ModelResponse(parts=[TextPart(content="too late")])

    httpd, deps, base = _serve(
        monkeypatch, tmp_path, FunctionModel(hang), settings_yaml="serve_timeout: 0.4\n"
    )
    try:
        r = httpx.post(f"{base}/task", json={"task": "go"}, timeout=10)
        assert r.status_code == 504
    finally:
        stop_background(httpd, deps)


def test_body_too_large_413(monkeypatch, tmp_path):
    # Raw socket: we declare an oversized Content-Length but never send the body,
    # so the server rejects on the header (before reading) and we read the reply
    # cleanly — an httpx upload would get RST mid-send on Windows.
    httpd, deps, base = _serve(monkeypatch, tmp_path, _text_model())
    port = int(base.rsplit(":", 1)[1])
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(
                b"POST /task HTTP/1.0\r\nHost: x\r\nContent-Length: 2000000\r\n\r\n"
            )
            resp = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                resp += chunk
        assert b"413" in resp.split(b"\r\n", 1)[0]
    finally:
        stop_background(httpd, deps)


def test_post_without_content_length_411(monkeypatch, tmp_path):
    httpd, deps, base = _serve(monkeypatch, tmp_path, _text_model())
    port = int(base.rsplit(":", 1)[1])
    try:
        # Raw request with no Content-Length and no body.
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"POST /task HTTP/1.0\r\nHost: x\r\n\r\n")
            resp = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                resp += chunk
        assert b"411" in resp.split(b"\r\n", 1)[0]
    finally:
        stop_background(httpd, deps)


def test_sse_stream_emits_frames(monkeypatch, tmp_path):
    # TestModel streams (FunctionModel would need a stream_function) and auto-calls
    # available tools — leave only list_dir so the stream has exactly one tool call.
    disable = "tools:\n  disable: [read_file, write_file, run_shell, fetch_url]\n"
    httpd, deps, base = _serve(monkeypatch, tmp_path, TestModel(), settings_yaml=disable)
    try:
        r = httpx.get(f"{base}/task/stream?q=list+files", timeout=10)
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        body = r.text
        assert "event: tool\n" in body          # the list_dir call
        assert "event: tool_result\n" in body   # its result
        assert "event: done\n" in body          # terminal frame
    finally:
        stop_background(httpd, deps)
