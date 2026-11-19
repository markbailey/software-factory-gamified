"""Runs one allowlisted command-line tool and streams its output.

The process runs in a worker thread so this works the same on Windows, macOS and
Linux whatever event loop the server uses.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Awaitable, Callable

TIMEOUT_EXIT_CODE = -9
STOPPED_EXIT_CODE = -15  # stopped because the backend is shutting down

# Running processes, so the backend can stop them all when it shuts down. Each runs in its own
# process group: Ctrl+C in the backend's window doesn't reach it (and fail its step); the
# backend stops it itself, and the crate carries on from that machine when it starts again.
_live: set[subprocess.Popen] = set()
_live_lock = threading.Lock()
_stopping = threading.Event()
_GROUP = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
          else {"start_new_session": True})


def stop_all() -> None:
    """Stop every running command (and what it started) for a shutdown."""
    _stopping.set()
    with _live_lock:
        procs = list(_live)
    for proc in procs:
        _kill_tree(proc)


def stopping() -> bool:
    return _stopping.is_set()


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":  # claude.cmd starts node: take the whole tree
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=15)
        else:
            os.killpg(proc.pid, 9)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class CommandNotAllowed(Exception):
    pass


def _base_name(exe: str) -> str:
    # "python3.12.exe" -> "python", "gh.exe" -> "gh", "claude.cmd" -> "claude"
    return re.sub(r"[\d.]+$", "", Path(exe).stem.lower())


def resolve(argv: list[str], allowed: list[str]) -> list[str]:
    """Check argv[0] against the allowlist and resolve it to a full path."""
    if not argv:
        raise CommandNotAllowed("empty command")
    exe = argv[0]
    if _base_name(exe) not in {a.lower() for a in allowed}:
        raise CommandNotAllowed(f"'{Path(exe).name}' is not on the allowlist")
    path = shutil.which(exe) or (exe if Path(exe).is_file() else None)
    if not path:
        raise FileNotFoundError(f"command not found: {exe}")
    return [path, *argv[1:]]


async def run(
    argv: list[str],
    cwd: Path,
    on_line: Callable[[str], Awaitable[None]],
    stdin_text: str | None = None,
    timeout: float | None = None,
    env: dict[str, str | None] | None = None,
) -> int:
    """Run argv in cwd, awaiting on_line for each output line. Returns the exit code. env: changes
    to this process's environment for it (None unsets a variable), e.g. which account it runs as."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    # PYTHONUTF8: our scripts read and write UTF-8 (Windows would otherwise use cp1252).
    changes = env or {}
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}
    for key, value in changes.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value

    def emit(kind: str, value) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (kind, value))

    def worker() -> None:
        code = -1
        timer = None
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **_GROUP,
            )
            with _live_lock:
                _live.add(proc)
            timed_out = threading.Event()
            if timeout:
                def kill() -> None:
                    timed_out.set()
                    _kill_tree(proc)
                timer = threading.Timer(timeout, kill)
                timer.start()
            if stdin_text is not None:
                assert proc.stdin is not None
                try:
                    proc.stdin.write(stdin_text)
                finally:
                    proc.stdin.close()
            assert proc.stdout is not None
            for line in proc.stdout:
                emit("line", line.rstrip("\r\n"))
            code = proc.wait()
            with _live_lock:
                _live.discard(proc)
            if _stopping.is_set():
                code = STOPPED_EXIT_CODE
            elif timed_out.is_set():
                emit("line", f"[runner] timed out after {timeout:.0f}s and was stopped")
                code = TIMEOUT_EXIT_CODE
        except Exception as exc:  # report launch failures as log lines
            emit("line", f"[runner] {exc}")
        finally:
            if timer:
                timer.cancel()
        emit("exit", code)

    threading.Thread(target=worker, daemon=True).start()
    while True:
        kind, value = await queue.get()
        if kind == "exit":
            return value
        await on_line(value)
