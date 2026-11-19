"""A machine waits for the crate to finish riding its belt in the game before it starts, but only
while a game that says so is connected, and never past the cap."""
import asyncio
import time

from factory.engine import Factory as Engine
from factory.hub import Hub


class FakeStore:
    def __init__(self, location="build:belt_1"):
        self.items = {1: {"id": 1, "location": location}}
        self.events = []

    def get_item(self, item_id):
        return self.items.get(item_id)

    def update_item(self, item_id, **fields):
        self.items[item_id].update(fields)

    def append_event(self, event):
        self.events.append(event)
        return len(self.events)


def engine(clients=1, cap=5.0, location="build:belt_1"):
    """Just the parts of the factory that crate arrivals use."""
    e = Engine.__new__(Engine)
    e.store = FakeStore(location)
    e.hub = Hub(e.store)
    for k in range(clients):
        e.hub.arrival_clients.add(f"client-{k}")
    e.settled, e._arrivals, e.arrival_wait_max = {}, {}, cap
    return e


def timed(coro):
    start = time.monotonic()
    asyncio.run(coro)
    return time.monotonic() - start


def test_no_wait_without_a_game_connected():
    e = engine(clients=0)
    assert timed(e._await_arrival(1)) < 0.1


def test_no_wait_when_the_game_already_shows_it_there():
    e = engine()
    e.crate_arrived(1, "build:belt_1")
    assert timed(e._await_arrival(1)) < 0.1


def test_waits_until_the_game_says_it_arrived():
    e = engine()

    async def run():
        async def arrive():
            await asyncio.sleep(0.2)
            e.crate_arrived(1, "build:intake")  # somewhere else: still waiting
            await asyncio.sleep(0.2)
            e.crate_arrived("1", "build:belt_1")
        task = asyncio.create_task(arrive())
        await e._await_arrival(1)
        await task
        assert not e._arrivals

    took = timed(run())
    assert 0.35 < took < 1.5


def test_gives_up_at_the_cap():
    e = engine(cap=0.3)
    took = timed(e._await_arrival(1))
    assert 0.25 < took < 1.0


def test_stops_waiting_when_the_game_disconnects():
    e = engine(cap=10.0)

    async def run():
        async def leave():
            await asyncio.sleep(0.2)
            e.hub.remove("client-0")
        task = asyncio.create_task(leave())
        await e._await_arrival(1)
        await task

    assert timed(run()) < 2.0


def test_a_move_forgets_where_the_game_last_showed_it():
    e = engine()
    e.crate_arrived(1, "build:belt_1")

    async def run():
        await e._move(1, "build:coder", 0.0, state="running")
        assert 1 not in e.settled
        await e._move(1, "build:belt_1", 0.0)  # back on the same belt (rework): a fresh arrival is needed
        e.arrival_wait_max = 0.2
        start = time.monotonic()
        await e._await_arrival(1)
        assert time.monotonic() - start >= 0.15

    asyncio.run(run())


def test_bad_arrivals_are_ignored():
    e = engine()
    e.crate_arrived("not a number", "x")
    e.crate_arrived(None, "x")
    assert e.settled == {}


# End to end, over the websocket like the game ------------------------------------------------

def test_the_coder_waits_for_the_game_to_show_the_crate_on_its_belt(factory):
    import json as _json
    factory.start()

    async def go():
        async with factory.client() as c:
            await c.send(type="crate_arrivals", on=True)
            n = await c.submit("Add a greeting")
            moved = await c.wait_for(lambda e: e.get("type") == "item_moved" and e.get("item_id") == n
                                     and e.get("to", "").endswith(":belt_0"))
            # The belt time passes, but the game still shows the crate riding: no job yet.
            await asyncio.sleep(moved["duration"] + 1.5)
            while True:
                try:
                    c.events.append(_json.loads(await asyncio.wait_for(c.ws.recv(), 0.2)))
                except asyncio.TimeoutError:
                    break
            assert not [e for e in c.events if e.get("type") == "job_started"]
            await c.send(type="crate_arrived", item_id=n, location=moved["to"])
            started = await c.wait_for(lambda e: e.get("type") == "job_started" and e.get("item_id") == n,
                                       timeout=10)
            assert started["machine"].endswith("coder")
    asyncio.run(go())


def test_a_game_that_never_says_arrived_only_holds_it_up_to_the_cap(factory):
    factory.start()

    async def go():
        async with factory.client() as c:
            await c.send(type="crate_arrivals", on=True)
            n = await c.submit("Add a greeting")
            await c.wait_for(lambda e: e.get("type") == "item_moved" and e.get("item_id") == n
                             and e.get("to", "").endswith(":belt_0"))
            start = time.monotonic()
            await c.wait_for(lambda e: e.get("type") == "job_started" and e.get("item_id") == n, timeout=60)
            return time.monotonic() - start
    took = asyncio.run(go())
    assert 15 < took < 40
