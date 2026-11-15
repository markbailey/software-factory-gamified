"""Agent health check: is Claude there to do the work?

Every few minutes (and straight after a Claude step fails) the backend asks Claude for a
one-word reply. While that fails, machines that run Claude hold their crates on the belt in
front of them, and a Claude step that failed because Claude went away runs again once it is
back. Machines that don't use Claude (tests, CI, merging) carry on. The game shows a banner
while Claude is unavailable, and everything resumes by itself when a check passes.

Settings live in [health] in factory.toml (all optional).
"""
from __future__ import annotations

import asyncio
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from . import runner
from .config import DEFAULT_CONFIG

# What a failed check's output usually means, in words for the banner.
REASONS = [
    (re.compile(r"not (logged|signed) in|/login|authenticat|invalid api key|401", re.I),
     "Claude is not signed in. Run `claude` in a terminal and sign in."),
    (re.compile(r"usage limit|rate limit|429|limit reached|quota|credit", re.I),
     "Claude's usage limit has been reached."),
    (re.compile(r"overloaded|529|503|500|internal server error", re.I),
     "Claude's service is overloaded or down."),
    (re.compile(r"ENOTFOUND|ECONNREFUSED|ETIMEDOUT|network|getaddrinfo|connection", re.I),
     "Can't reach Claude: check the internet connection."),
    (re.compile(r"command not found|not on the allowlist|No such file", re.I),
     "The claude command isn't installed or isn't on the PATH."),
    (re.compile(r"timed out", re.I), "Claude didn't answer in time."),
]


@dataclass
class HealthConfig:
    enabled: bool = True
    # The check: a tiny request that needs a working, signed-in Claude.
    command: list[str] = field(default_factory=lambda: [
        "claude", "-p", "Reply with the single word OK.", "--model", "haiku"])
    interval_seconds: float = 300.0  # between checks while Claude is fine
    retry_seconds: float = 30.0  # between checks while it isn't
    timeout_seconds: float = 90.0

    @classmethod
    def from_toml(cls, path: Path = DEFAULT_CONFIG) -> "HealthConfig":
        with open(path, "rb") as f:
            raw = tomllib.load(f).get("health", {})
        cfg = cls()
        cfg.enabled = bool(raw.get("enabled", cfg.enabled))
        if isinstance(raw.get("command"), list) and raw["command"]:
            cfg.command = [str(x) for x in raw["command"]]
        cfg.interval_seconds = float(raw.get("interval_seconds", cfg.interval_seconds))
        cfg.retry_seconds = float(raw.get("retry_seconds", cfg.retry_seconds))
        cfg.timeout_seconds = float(raw.get("timeout_seconds", cfg.timeout_seconds))
        return cfg


class AgentHealth:
    def __init__(self, cfg: HealthConfig, allowed_commands: list[str], cwd: Path,
                 broadcast: Callable[[dict], Awaitable[None]]):
        self.cfg = cfg
        self.allowed = allowed_commands
        self.cwd = cwd
        self.broadcast = broadcast
        self.available = True  # assume fine until a check says otherwise
        self.reason = ""
        self.detail = ""
        self.since = time.time()
        self.checked_at = 0.0
        self.next_check_at = 0.0
        self._ok = asyncio.Event()
        self._ok.set()
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()

    def status(self) -> dict:
        return {"type": "agent_status", "available": self.available, "reason": self.reason,
                "detail": self.detail, "since": self.since, "checked_at": self.checked_at,
                "next_check_at": self.next_check_at, "enabled": self.cfg.enabled}

    async def wait_available(self) -> None:
        await self._ok.wait()

    async def check_now(self) -> bool:
        """Run a check (or join one already running). Returns True if Claude is available."""
        if not self.cfg.enabled:
            return True
        if self._lock.locked():
            async with self._lock:
                return self.available
        async with self._lock:
            ok, output = await self._probe()
            await self._set(ok, output)
            return ok

    def poke(self) -> None:
        """Check again now (from the game's "Check now" button)."""
        self._wake.set()

    async def run(self) -> None:
        if not self.cfg.enabled:
            return
        while True:
            await self.check_now()
            wait = self.cfg.interval_seconds if self.available else self.cfg.retry_seconds
            self.next_check_at = time.time() + wait
            await self.broadcast(self.status())
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    async def _probe(self) -> tuple[bool, str]:
        lines: list[str] = []

        async def collect(line: str) -> None:
            lines.append(line)

        try:
            argv = runner.resolve(self.cfg.command, self.allowed)
        except (runner.CommandNotAllowed, FileNotFoundError) as exc:
            return False, str(exc)
        code = await runner.run(argv, self.cwd, collect, timeout=self.cfg.timeout_seconds)
        text = "\n".join(lines).strip()
        return code == 0 and bool(text), text or f"exit code {code}"

    async def _set(self, ok: bool, output: str) -> None:
        self.checked_at = time.time()
        changed = ok != self.available
        if changed:
            self.since = self.checked_at
        self.available = ok
        if ok:
            self.reason, self.detail = "", ""
            self._ok.set()
        else:
            self.reason = _reason(output)
            self.detail = _tail(output)
            self._ok.clear()
        if changed:
            word = "back" if ok else f"unavailable: {self.reason}"
            print(f"[health] Claude is {word}")
            await self.broadcast(self.status())


def _reason(output: str) -> str:
    for pattern, words in REASONS:
        if pattern.search(output):
            return words
    return "Claude didn't answer the health check."


def _tail(output: str) -> str:
    lines = [l for l in output.splitlines() if l.strip()]
    return "\n".join(lines[-4:])[-400:]
