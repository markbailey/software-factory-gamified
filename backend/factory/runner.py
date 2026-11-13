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
) -> int:
    """Run argv in cwd, awaiting on_line for each output line. Returns the exit code."""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}

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
            )
            timed_out = threading.Event()
            if timeout:
                def kill() -> None:
                    timed_out.set()
                    proc.kill()
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
            if timed_out.is_set():
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
