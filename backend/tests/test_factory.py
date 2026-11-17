"""End-to-end tests: a real backend with pretend `claude` and `gh`, driven over its websocket.

Slow-ish (each starts a backend); run them all with:  .venv\\Scripts\\python -m pytest tests
"""
import asyncio

import pytest

from .conftest import IS_WINDOWS, item, simple_layout  # noqa: F401

END = ("shipped", "held", "scrapped")


def run(coro):
    return asyncio.run(coro)


def ships(factory, n):
    crate = item(factory, n)
    assert crate["state"] == "shipped", [e for e in crate["log"]][-12:]


def test_a_crate_ships(factory):
    factory.start()

    async def go():
        async with factory.client() as c:
            n = await c.submit("Add a greeting")
            await c.until_state(n, *END)
            return n
    ships(factory, run(go()))


def test_four_crates_at_once_all_ship(factory):
    """Parallel crates share one repo: none trips over another's branch or the base branch."""
    factory.start()

    async def go():
        async with factory.client() as c:
            ids = [await c.submit(f"Change number {k}") for k in range(4)]
            left = set(ids)
            while left:
                e = await c.wait_for(lambda e: e.get("type") == "item_moved" and e.get("item_id") in left
                                     and e.get("state") in END, timeout=180)
                left.discard(e["item_id"])
            return ids
    for n in run(go()):
        ships(factory, n)


def test_killed_mid_coder_resumes_and_ships(factory):
    """The backend dies while the Coder works: on restart the crate carries on, reusing its branch."""
    factory.flag("slow_coder", "30")
    factory.start()

    async def first():
        async with factory.client() as c:
            n = await c.submit("Survive a crash")
            await c.until_log(n, "claude")
            await asyncio.sleep(2)
            return n
    n = run(first())
    factory.stop(gently=False)
    factory.unflag("slow_coder")
    factory.start()

    async def second():
        async with factory.client() as c:
            if item(factory, n)["state"] not in END:
                await c.until_state(n, *END, timeout=120)
    run(second())
    ships(factory, n)
    assert "already exists" not in factory.log()


@pytest.mark.skipif(IS_WINDOWS, reason="a test can't press Ctrl+C in another window on Windows")
def test_ctrl_c_mid_step_does_not_hold_the_crate(factory):
    """Ctrl+C in the backend's window stops it without the crate being held as failed."""
    factory.flag("slow_coder", "30")
    factory.start()

    async def first():
        async with factory.client() as c:
            n = await c.submit("Survive Ctrl+C")
            await c.until_log(n, "claude")
            await asyncio.sleep(2)
            return n
    n = run(first())
    factory.stop(gently=True)
    assert factory.proc.returncode is not None
    assert item(factory, n)["state"] != "held", item(factory, n)["log"][-6:]
    factory.unflag("slow_coder")
    factory.start()

    async def second():
        async with factory.client() as c:
            if item(factory, n)["state"] not in END:
                await c.until_state(n, *END, timeout=120)
    run(second())
    ships(factory, n)


def test_paused_machine_holds_crates_in_front_until_resumed(factory):
    factory.start()

    async def go():
        async with factory.client() as c:
            await c.send(type="pause", target="machine", id="tester", paused=True)
            await c.wait_for(lambda e: e.get("type") == "pauses")
            n = await c.submit("Wait for the tester")
            await c.wait_for(lambda e: e.get("type") == "job_finished" and e.get("machine") == "coder")
            await asyncio.sleep(3)
            assert item(factory, n)["state"] == "waiting"
            assert not any(e.get("type") == "job_started" and e.get("machine") == "tester" for e in c.events)
            await c.send(type="pause", target="machine", id="tester", paused=False)
            await c.until_state(n, *END)
            return n
    ships(factory, run(go()))


def test_save_while_running_drops_a_machine(factory):
    """Saving a layout while a crate runs: the Tester goes, the crate skips it and still ships."""
    factory.flag("slow_coder", "4")
    factory.start()

    async def go():
        async with factory.client() as c:
            n = await c.submit("Layout changes under me")
            await c.until_log(n, "claude")
            await c.save([simple_layout(["coder", "packager"])])
            await c.until_state(n, *END, timeout=120)
            assert not any(e.get("type") == "job_started" and e.get("machine") == "tester" for e in c.events)
            return n
    ships(factory, run(go()))


def test_removed_line_parks_its_crate_until_the_line_comes_back(factory):
    factory.flag("slow_coder", "4")
    factory.start()
    other = simple_layout(["coder", "packager"], pid="other", name="Other", row=10)
    for m in other["machines"]:
        m["id"] = "o_" + m["id"]
    for b in other["belts"]:
        b["from"] = b["from"] if b["from"] in ("intake", "dock") else "o_" + b["from"]
        b["to"] = b["to"] if b["to"] in ("intake", "dock") else "o_" + b["to"]

    async def go():
        async with factory.client() as c:
            n = await c.submit("Lose my line")
            await c.until_log(n, "claude")
            await c.save([other])
            await c.until_log(n, "until the line is back", timeout=30)
            await c.save([simple_layout(), other])
            await c.until_state(n, "shipped", "scrapped", timeout=120)
            return n
    ships(factory, run(go()))


def test_held_crate_follows_a_red_belt_laid_afterwards(factory):
    """Tests fail, the crate is held at the Tester; laying a red belt back to the Coder sends it there."""
    factory.flag("fail_tests")
    factory.start()

    async def go():
        async with factory.client() as c:
            n = await c.submit("Fail then fix")
            await c.until_state(n, "held", timeout=90)
            factory.unflag("fail_tests")
            layout = simple_layout()
            layout["belts"].append({"from": "tester", "port": "fail", "to": "coder"})
            await c.save([layout])
            await c.until_state(n, "shipped", "scrapped", timeout=120)
            return n
    ships(factory, run(go()))


def test_line_limit_lets_one_crate_on_at_a_time(make_factory):
    """max_crates 1: typed specs wait at the intake until the crate ahead leaves the line."""
    layout = simple_layout()
    layout["max_crates"] = 1
    factory = make_factory(layout=[layout])
    factory.flag("slow_coder", "2")
    factory.start()

    async def go():
        async with factory.client() as c:
            ids = [await c.submit(f"Crate {k}") for k in range(3)]
            on_line: set = set()
            most = 0
            left = set(ids)
            while left:
                e = await c.wait_for(lambda e: e.get("type") in ("item_moved", "item_waiting"), timeout=180)
                if e.get("type") == "item_waiting":
                    continue
                state, n = e.get("state"), e["item_id"]
                if state in ("waiting", "running", "moving", "awaiting_approval"):
                    on_line.add(n)
                elif state in END:
                    on_line.discard(n)
                    left.discard(n)
                most = max(most, len(on_line))
            waits = [e for e in c.events if e.get("type") == "item_waiting" and "line is full" in e.get("reason", "")]
            return ids, most, waits
    ids, most, waits = run(go())
    assert most == 1
    assert waits, "the crates behind should have said why they wait"
    for n in ids:
        ships(factory, n)


def test_coder_retries_a_failed_crate_by_itself(make_factory):
    """retries 2 on the Coder: a crate whose Coder run fails goes round the Coder again."""
    layout = simple_layout()
    layout["machines"][0]["retries"] = 2
    factory = make_factory(layout=[layout])
    factory.flag("coder_fails", "1")  # the next Coder run fails, then they work
    factory.start()

    async def go():
        async with factory.client() as c:
            n = await c.submit("Fail once")
            await c.until_state(n, *END, timeout=120)
            return n
    n = run(go())
    ships(factory, n)
    assert any("retry 1 of 2" in line for line in item(factory, n)["log"])
    assert sum(1 for line in item(factory, n)["log"] if line.startswith("== Coder 4/")) == 2


def test_retries_run_out_then_the_crate_is_held(make_factory):
    layout = simple_layout()
    layout["machines"][0]["retries"] = 1
    factory = make_factory(layout=[layout])
    factory.flag("coder_fails", "9")
    factory.start()

    async def go():
        async with factory.client() as c:
            n = await c.submit("Always fails")
            await c.until_state(n, "held", timeout=120)
            return n
    n = run(go())
    assert sum(1 for line in item(factory, n)["log"] if line.startswith("== Coder 4/")) == 2


def test_crates_take_as_long_as_the_belt_at_the_belt_speed(make_factory):
    layout = simple_layout()
    for b in layout["belts"]:
        if b["from"] == "coder":
            b["path"] = [[5, 0], [6, 0], [7, 0], [8, 0]]  # 4 cells
    factory = make_factory(layout=[layout])
    factory.start()

    async def go():
        async with factory.client() as c:
            assert c.snapshot["layout"]["belt_speed"] == 1.5
            await c.send(type="belt_speed", speed=6)
            e = await c.wait_for(lambda e: e.get("type") == "belt_speed")
            assert e["speed"] == 6
            n = await c.submit("Ride the belt")
            moved = await c.wait_for(lambda e: e.get("type") == "item_moved" and e.get("to") == "build:belt_1")
            await c.until_state(n, *END)
            return n, moved
    n, moved = run(go())
    assert abs(moved["duration"] - 4 / 6) < 0.01
    ships(factory, n)
    assert "\"speed\": 6" in (factory.backend / "belts.json").read_text()


def test_crate_for_another_machine_is_carried_there(make_factory):
    """The Refiner says the crate needs a Coder: a worker carries it to the line's Coder, and it ships."""
    factory = make_factory(layout=[simple_layout(["refiner", "coder", "tester", "packager"])])
    factory.flag("wrong_refiner", "coder")
    factory.start()

    async def go():
        async with factory.client() as c:
            n = await c.submit("Not refiner work")
            await c.until_state(n, *END, timeout=120)
            return n, [e for e in c.events if e.get("type") == "item_moved" and e.get("carried")]
    n, carried = run(go())
    ships(factory, n)
    assert carried and carried[0]["from"] == "refiner" and carried[0]["machine"] == "coder"
    assert any("a worker carries it to the Coder" in line for line in item(factory, n)["log"])


def test_crate_the_line_cant_handle_is_rolled_back_into_the_bin(make_factory):
    """The Coder says it needs a Merger and the line has none: it goes in the scrap bin, labelled."""
    factory = make_factory(layout=[simple_layout()])
    factory.flag("wrong_coder", "merger")
    factory.start()

    async def go():
        async with factory.client() as c:
            n = await c.submit("Needs merging")
            await c.until_state(n, "scrapped", timeout=120)
            return n
    n = run(go())
    crate = item(factory, n)
    assert crate["scrap_kind"] == "wrong_line", crate["log"][-8:]
    assert crate["outcome"] == "wrong_machine:merger"
    assert not any(line.startswith("== Coder 5/") for line in crate["log"]), "it should skip the Coder's other steps"


def test_extra_docks_and_bins(make_factory):
    """A line ships into a second dock, and a crate it can't handle rolls back along the Coder's
    red belt into a second bin; stats count both."""
    layout = simple_layout()
    layout["docks"] = [{"id": "dock_2", "pos": [16, 0]}]
    layout["bins"] = [{"id": "scrap_2", "pos": [16, 4]}]
    for b in layout["belts"]:
        if b["to"] == "dock":
            b["to"] = "dock_2"
    layout["belts"].append({"from": "coder", "port": "fail", "to": "scrap_2"})
    factory = make_factory(layout=[layout])
    factory.start()

    async def go():
        async with factory.client() as c:
            assert [d["id"] for d in c.snapshot["layout"]["pipelines"][0]["docks"]] == ["dock_2"]
            a = await c.submit("Ships into dock 2")
            await c.until_state(a, *END, timeout=120)
            factory.flag("wrong_coder", "merger")
            b = await c.submit("Needs merging")
            await c.until_state(b, *END, timeout=120)
            await c.send(type="stats", days=7)
            stats = await c.wait_for(lambda e: e.get("type") == "stats")
            return a, b, stats
    a, b, stats = run(go())
    ships(factory, a)
    assert item(factory, a)["location"] == "build:dock_2"
    crate = item(factory, b)
    assert crate["state"] == "scrapped" and crate["scrap_kind"] == "wrong_line", crate["log"][-8:]
    assert crate["location"] == "build:scrap_2"
    assert stats["shipped"] == 1 and stats["scrapped"] == 1


def test_free_conveyors_are_kept(make_factory):
    """Conveyors laid by hand that link nothing are saved with the line (a broken run is cut
    where it stops following on), and a crate still ships past them."""
    layout = simple_layout()
    layout["conveyors"] = [
        {"id": "conv_1", "cells": [[2, 8], [3, 8], [4, 8], [9, 9]]},
        {"id": "conv_2", "cells": [[2, 10], [3, 10], [3, 11], [2, 11]], "loop": True},
        {"id": "junk", "cells": "nope"},
    ]
    factory = make_factory(layout=[layout])
    factory.start()

    async def go():
        async with factory.client() as c:
            await c.send(type="save_pipelines", pipelines=[layout])
            snap = await c.wait_for(lambda e: e.get("type") == "snapshot", timeout=30)
            convs = snap["layout"]["pipelines"][0]["conveyors"]
            a = await c.submit("Ships past the conveyors")
            await c.until_state(a, *END, timeout=120)
            return convs, a
    convs, a = run(go())
    assert convs == [{"id": "conv_1", "cells": [[2, 8], [3, 8], [4, 8]]},
                     {"id": "conv_2", "cells": [[2, 10], [3, 10], [3, 11], [2, 11]], "loop": True}]
    ships(factory, a)


def test_splitter_takes_turns_and_merger_joins(make_factory):
    """A splitter sends crates out of its two outputs in turn, along two Coders, and a belt
    merger joins both belts into the rest of the line."""
    layout = simple_layout(machines=["refiner"])
    layout["machines"] += [
        {"id": "split", "type": "splitter", "pos": [6, 0]},
        {"id": "coder", "type": "coder", "pos": [9, -3]},
        {"id": "coder-2", "type": "coder", "pos": [9, 3]},
        {"id": "join", "type": "joiner", "pos": [12, 0]},
    ]
    layout["dock"]["pos"] = [15, 0]
    layout["belts"] = [
        {"from": "intake", "port": "out", "to": "refiner"},
        {"from": "refiner", "port": "pass", "to": "split"},
        {"from": "split", "port": "out_3", "to": "coder"},
        {"from": "split", "port": "out_4", "to": "coder-2"},
        {"from": "coder", "port": "pass", "to": "join"},
        {"from": "coder-2", "port": "pass", "to": "join"},
        {"from": "join", "port": "pass", "to": "dock"},
    ]
    factory = make_factory(layout=[layout])
    factory.start()

    async def go():
        async with factory.client() as c:
            assert not c.snapshot["layout"]["pipelines"][0].get("problem"), c.snapshot["layout"]["pipelines"][0]
            a = await c.submit("First crate")
            await c.until_state(a, *END, timeout=120)
            b = await c.submit("Second crate")
            await c.until_state(b, *END, timeout=120)
            return a, b
    a, b = run(go())
    ships(factory, a)
    ships(factory, b)
    logs = [" ".join(str(e) for e in item(factory, n)["log"]) for n in (a, b)]
    assert "out of output 3" in logs[0] and "out of output 4" in logs[1], logs
