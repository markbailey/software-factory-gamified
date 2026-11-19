"""Scrap now: any crate that hasn't shipped or been scrapped can be thrown away, wherever it is.
Its work (and the command it's running) stops, and it goes into the scrap bin."""
from __future__ import annotations

import asyncio
import sys
import time
from types import SimpleNamespace

from factory import runner
from factory.engine import Factory as Engine


class _Store:
    def __init__(self, item):
        self.item = item

    def get_item(self, item_id):
        return self.item

    def update_item(self, item_id, **fields):
        self.item.update(fields)


class _Hub:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


def _engine(state="running", location="coder"):
    e = Engine.__new__(Engine)
    e.store = _Store({"id": 7, "state": state, "location": location})
    e.hub = _Hub()
    e.decisions, e.item_tasks, e.running_on = {}, {}, {}
    e.lines = {"l": SimpleNamespace(machines=[SimpleNamespace(id="coder", name="Coder")])}
    e.logged, e.scrapped = [], []

    async def log(item_id, machine, line):
        e.logged.append(line)

    async def scrap(item_id, reason, output="", kind="work_failed", via="", into="scrap"):
        e.scrapped.append(reason)
        e.store.item["state"] = "scrapped"
    e._log, e._scrap = log, scrap
    return e


def test_scrap_now_stops_a_running_crate_and_scraps_it():
    async def go():
        e = _engine()
        stopped = asyncio.Event()

        async def work():
            try:
                await asyncio.sleep(60)
            finally:
                stopped.set()
        e.item_tasks[7] = asyncio.create_task(work())
        await asyncio.sleep(0)
        assert await e.decide(7, "scrap") is None  # the Scrap button, not waiting at a gate
        assert stopped.is_set()
        assert e.scrapped == ["Coder: scrapped by you"]
        assert any(ev["type"] == "job_failed" for ev in e.hub.events)
    asyncio.run(go())


def test_the_machine_is_told_it_stopped_even_if_the_crate_says_waiting():
    async def go():
        e = _engine(state="waiting", location="coder")  # e.g. waiting for Claude inside the machine
        e.running_on[7] = "coder"
        assert await e.scrap_now(7) is None
        failed = [ev for ev in e.hub.events if ev["type"] == "job_failed"]
        assert failed and failed[0]["machine"] == "coder"
        assert 7 not in e.running_on
    asyncio.run(go())


def test_finished_crates_cant_be_scrapped_and_approve_still_needs_a_gate():
    e = _engine(state="shipped")
    assert "already shipped" in asyncio.run(e.scrap_now(7))
    e = _engine()
    assert "not waiting for approval" in asyncio.run(e.decide(7, "approve"))


def test_a_stopped_step_kills_its_command():
    async def go():
        lines = []
        started = asyncio.Event()

        async def on_line(line):
            lines.append(line)
            started.set()
        argv = [sys.executable, "-c", "import time; print('go', flush=True); time.sleep(60)"]
        task = asyncio.create_task(runner.run(argv, ".", on_line, timeout=120))
        await asyncio.wait_for(started.wait(), 20)
        t0 = time.monotonic()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert time.monotonic() - t0 < 10
        await asyncio.sleep(0.5)
        with runner._live_lock:
            assert all(p.poll() is not None for p in runner._live)
    asyncio.run(go())
