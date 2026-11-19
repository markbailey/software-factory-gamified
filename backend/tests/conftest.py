"""A whole factory for the tests: the backend in a temp folder, a local git remote, stand-in
`claude` and `gh` commands (tests/fakes), and a websocket client to drive it like the game.

Run from backend/:  .venv\\Scripts\\python -m pytest tests     (or .venv/bin/python on macOS/Linux)
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
FAKES = Path(__file__).resolve().parent / "fakes"
IS_WINDOWS = os.name == "nt"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _shim(bin_dir: Path, name: str, script: Path) -> None:
    """A command `name` on PATH that runs script with this Python."""
    if IS_WINDOWS:
        (bin_dir / f"{name}.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        path = bin_dir / name
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        path.chmod(0o755)


LINE_MACHINES = ["coder", "tester", "packager"]


def simple_layout(machines=LINE_MACHINES, pid="build", name="Build", row=0) -> dict:
    """One line: intake, the machines in a row, dock and scrap bin; failed crates wait (no red belts)."""
    ms = [{"id": m, "type": m, "pos": [3 + 3 * k, row]} for k, m in enumerate(machines)]
    order = ["intake", *machines, "dock"]
    belts = [{"from": a, "port": "out" if a == "intake" else "pass", "to": b} for a, b in zip(order, order[1:])]
    return {"id": pid, "name": name, "sources": ["game"], "storages": [], "repo": {"path": "test-project"},
            "intake": {"pos": [0, row]}, "dock": {"pos": [3 + 3 * len(machines), row]},
            "scrap": {"pos": [3 + 3 * len(machines), row + 3]}, "machines": ms, "belts": belts}


class Factory:
    """The backend running in a temp folder, plus a client to talk to it."""

    def __init__(self, root: Path):
        self.root = root
        self.backend = root / "backend"
        self.state = root / "state"
        self.port = _free_port()
        self.proc: subprocess.Popen | None = None

    # Set up -----------------------------------------------------------------
    def build(self, layout: list[dict], extra_toml: str = "") -> None:
        shutil.copytree(BACKEND / "factory", self.backend / "factory",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(BACKEND / "scripts", self.backend / "scripts",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(BACKEND / "machines.toml", self.backend / "machines.toml")
        self.state.mkdir()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        _shim(bin_dir, "claude", FAKES / "fake_claude.py")
        _shim(bin_dir, "gh", FAKES / "fake_gh.py")
        remote = self.root / "remote.git"
        seed = self.root / "seed"
        seed.mkdir()
        _git(seed, "init", "-q", "-b", "main")
        (seed / "README.md").write_text("# Test project\n", encoding="utf-8")
        # The project's test, which the Tester runs: it fails while fail_tests is flagged. It is
        # here from the start because a Coder in Implement mode may not write tests.
        (seed / "tests").mkdir()
        (seed / "tests" / "test_work.py").write_text(
            "import os, unittest\n\nclass T(unittest.TestCase):\n    def test_flag(self):\n"
            f"        self.assertFalse(os.path.exists({(self.state / 'fail_tests').as_posix()!r}))\n",
            encoding="utf-8")
        _git(seed, "add", "-A")
        _git(seed, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "first")
        _git(self.root, "clone", "-q", "--bare", str(seed), str(remote))
        (self.backend / "factory.toml").write_text(f'''
[server]
host = "127.0.0.1"
port = {self.port}

[workspace]
path = "../workspace"
repo = "test-project"
remote = "{remote.as_posix()}"
worktrees = "worktrees"
base_branch = "main"

[security]
allowed_commands = ["python", "git", "gh", "claude"]

[health]
enabled = false

[belts]
travel_seconds = 0.1

[cleanup]
interval_minutes = 600
keep_scrapped_hours = 24
''' + extra_toml, encoding="utf-8")
        self.write_layout(layout)

    def write_layout(self, layout: list[dict]) -> None:
        (self.backend / "pipelines.json").write_text(json.dumps({"version": 5, "pipelines": layout}),
                                                     encoding="utf-8")

    def flag(self, name: str, value: str = "") -> None:
        (self.state / name).write_text(value, encoding="utf-8")

    def unflag(self, name: str) -> None:
        (self.state / name).unlink(missing_ok=True)

    # Run --------------------------------------------------------------------
    def start(self) -> None:
        env = {**os.environ, "FAKE_STATE": str(self.state), "PYTHONUTF8": "1",
               "PATH": str(self.root / "bin") + os.pathsep + os.environ.get("PATH", "")}
        seen = len(self.log())
        log = open(self.root / "server.log", "a", encoding="utf-8")
        kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS else {"start_new_session": True}
        self.proc = subprocess.Popen([sys.executable, "-m", "factory"], cwd=self.backend, env=env,
                                     stdout=log, stderr=subprocess.STDOUT, **kwargs)
        deadline = time.time() + 60
        while time.time() < deadline:
            if "startup complete" in self.log()[seen:]:
                return
            if self.proc.poll() is not None:
                break
            time.sleep(0.2)
        raise RuntimeError("the backend didn't start:\n" + self.log()[-3000:])

    def stop(self, gently: bool = True) -> None:
        """Stop the backend: like Ctrl+C in its window (gently) or killed outright."""
        if self.proc is None or self.proc.poll() is not None:
            return
        if gently:
            self.proc.send_signal(signal.CTRL_BREAK_EVENT if IS_WINDOWS else signal.SIGINT)
        else:
            self.proc.kill()
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)

    def log(self) -> str:
        path = self.root / "server.log"
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    def client(self) -> "Client":
        return Client(self.port)


class Client:
    """Talks to the backend over its websocket, as the game does."""

    def __init__(self, port: int):
        self.port = port

    async def __aenter__(self):
        import websockets
        self.ws = await websockets.connect(f"ws://127.0.0.1:{self.port}/ws", max_size=None)
        self.snapshot = json.loads(await self.ws.recv())
        self.events: list[dict] = []
        return self

    async def __aexit__(self, *exc):
        await self.ws.close()

    async def send(self, **msg) -> None:
        await self.ws.send(json.dumps(msg))

    async def wait_for(self, test, timeout: float = 60.0, approve: bool = True) -> dict:
        """The first event (from now) that passes test(event); approves gates on the way."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                raise AssertionError(f"timed out; last events: {self.events[-8:]}")
            try:
                event = json.loads(await asyncio.wait_for(self.ws.recv(), left))
            except asyncio.TimeoutError:
                continue
            self.events.append(event)
            if event.get("type") == "error":
                raise AssertionError(f"backend error: {event.get('message')}")
            if approve and event.get("type") == "item_moved" and event.get("state") == "awaiting_approval":
                await self.send(type="approve", item_id=event["item_id"])
            if test(event):
                return event

    async def submit(self, text: str, pipeline: str = "build") -> int:
        await self.send(type="submit_work", text=text, pipeline=pipeline)
        event = await self.wait_for(lambda e: e.get("type") == "item_created")
        return int(event["item"]["id"])

    async def until_state(self, item_id: int, *states: str, timeout: float = 90.0) -> dict:
        return await self.wait_for(lambda e: e.get("type") == "item_moved" and e.get("item_id") == item_id
                                   and e.get("state") in states, timeout)

    async def until_log(self, item_id: int, words: str, timeout: float = 60.0) -> dict:
        return await self.wait_for(lambda e: e.get("type") == "job_log" and e.get("item_id") == item_id
                                   and words in e.get("line", ""), timeout)

    async def save(self, layout: list[dict]) -> None:
        await self.send(type="save_pipelines", pipelines=layout)
        await self.wait_for(lambda e: e.get("type") == "snapshot", timeout=30)


@pytest.fixture
def factory(tmp_path):
    f = Factory(tmp_path)
    f.build([simple_layout()])
    yield f
    f.stop(gently=False)


@pytest.fixture
def make_factory(tmp_path):
    """make_factory(extra_toml) -> a Factory with more settings in its factory.toml."""
    made = []

    def make(extra_toml: str = "", layout=None) -> Factory:
        f = Factory(tmp_path)
        f.build(layout or [simple_layout()], extra_toml)
        made.append(f)
        return f
    yield make
    for f in made:
        f.stop(gently=False)


def item(factory_: Factory, item_id: int) -> dict:
    """A crate as the database has it."""
    import sqlite3
    db = sqlite3.connect(factory_.backend / "factory.db")
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    db.close()
    out = dict(row)
    out["log"] = json.loads(out["log"])
    return out
