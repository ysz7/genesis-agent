"""Gateway process lifecycle — start / stop / status (Phase 22e).

A running channel is its **own** OS process (``agent --gateway <name>``), tracked
by a PID file under ``workspace/gateways/<name>.pid`` with output tee'd to
``<name>.log``. That's what lets a bot started from the menu keep running after
you leave the menu, and run truly in parallel with the CLI (both share the same
SQLite/WAL store).

Cross-platform with no extra dependency: liveness and termination use ``os.kill``
on POSIX and a tiny ``ctypes`` shim on Windows (``os.kill(pid, 0)`` is unsafe on
Windows — it can terminate the target — so it is never used here).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("agent.gateways.manager")

_IS_WINDOWS = os.name == "nt"


# ── PID file helpers ──────────────────────────────────────────────────────────

def _dir(config: Any) -> Path:
    d = Path(config.workspace) / "gateways"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pid_path(config: Any, name: str) -> Path:
    return _dir(config) / f"{name}.pid"


def log_path(config: Any, name: str) -> Path:
    return _dir(config) / f"{name}.log"


def _read_pid(config: Any, name: str) -> int | None:
    path = pid_path(config, name)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _write_pid(config: Any, name: str, pid: int) -> None:
    pid_path(config, name).write_text(str(pid), encoding="utf-8")


def _clear_pid(config: Any, name: str) -> None:
    try:
        pid_path(config, name).unlink()
    except OSError:
        pass


# ── cross-platform process primitives ─────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    """True if a process with *pid* is currently running."""
    if pid <= 0:
        return False
    if _IS_WINDOWS:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists but owned by another user
    return True


def _terminate(pid: int) -> None:
    """Ask the process to stop (TerminateProcess on Windows, SIGTERM on POSIX)."""
    if _IS_WINDOWS:
        import ctypes

        PROCESS_TERMINATE = 0x0001
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle:
            try:
                kernel32.TerminateProcess(handle, 1)
            finally:
                kernel32.CloseHandle(handle)
        return
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


# ── launch ────────────────────────────────────────────────────────────────────

def _spawn_argv(config: Any, name: str) -> list[str]:
    """The command that runs one gateway in its own process."""
    return [sys.executable, "-m", "agent", "--gateway", name, "--root", str(config.root)]


def _launch(argv: list[str], log: Path) -> int:
    """Start *argv* as an independent process; return the child PID.

    Windows: a **new console window** (``CREATE_NEW_CONSOLE``) so the child's live
    monitor is visible and it survives the parent shell; the child also appends to
    *log* via its own file handler. POSIX: a detached session with output to
    ``/dev/null`` (the child's file handler persists the log) — no window.
    """
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL}
    if _IS_WINDOWS:
        kwargs["creationflags"] = 0x00000010 | 0x00000200  # CREATE_NEW_CONSOLE | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
    proc = subprocess.Popen(argv, **kwargs)
    return proc.pid


# ── public API ────────────────────────────────────────────────────────────────

def status(config: Any, name: str) -> dict:
    """``{"running": bool, "pid": int|None}`` for channel *name*.

    A stale PID file (process gone) is cleaned up and reported as not running.
    """
    pid = _read_pid(config, name)
    if pid is not None and _pid_alive(pid):
        return {"running": True, "pid": pid}
    if pid is not None:
        _clear_pid(config, name)
    return {"running": False, "pid": None}


def start(config: Any, name: str) -> dict:
    """Start channel *name* if not already running. Returns its :func:`status`."""
    current = status(config, name)
    if current["running"]:
        return current
    pid = _launch(_spawn_argv(config, name), log_path(config, name))
    _write_pid(config, name, pid)
    return {"running": True, "pid": pid}


def stop(config: Any, name: str) -> bool:
    """Stop channel *name*. Returns True if a running process was signalled."""
    pid = _read_pid(config, name)
    _clear_pid(config, name)
    if pid is not None and _pid_alive(pid):
        _terminate(pid)
        return True
    return False
