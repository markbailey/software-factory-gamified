"""End-to-end tests: a real backend with pretend `claude` and `gh`, driven over its websocket.

Slow-ish (each starts a backend); run them all with:  .venv\\Scripts\\python -m pytest tests
"""
import asyncio
import json

import pytest

from .conftest import IS_WINDOWS, item, simple_layout  # noqa: F401

END = ("shipped", "held", "scrapped")


def run(coro):
    return asyncio.run(coro)


def always_test(layout):
    """The Tester runs the project's tests even for a crate that changes none (a Coder in
    Implement mode writes no tests, and the Tester skips those by default)."""
    for m in layout["machines"]:
        if m["type"] == "tester":
            m["options"] = {**m.get("options", {}), "skip_without_tests": False}
    return layout


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
    factory.write_layout([always_test(simple_layout())])
    factory.flag("fail_tests")
    factory.start()

    async def go():
        async with factory.client() as c:
            n = await c.submit("Fail then fix")
            await c.until_state(n, "held", timeout=90)
            factory.unflag("fail_tests")
            layout = always_test(simple_layout())
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


def test_crates_waiting_for_approval_dont_fill_the_line(make_factory):
    """max_crates 1 with an Approver: crates waiting there for you don't count, so the next
    crate comes onto the line and joins them; each leaves when you decide, in any order."""
    layout = simple_layout(["coder", "approver", "tester"])
    layout["max_crates"] = 1
    factory = make_factory(layout=[layout])
    factory.start()

    async def go():
        async with factory.client() as c:
            ids = [await c.submit(f"Crate {k}") for k in range(3)]
            waiting: set = set()
            while len(waiting) < 3:
                e = await c.wait_for(lambda e: e.get("type") == "item_moved", timeout=180, approve=False)
                if e.get("state") == "awaiting_approval":
                    waiting.add(e["item_id"])
            # All three wait at the Approver at once; decide the last one first.
            await c.send(type="reject", item_id=ids[2], note="not this one")
            await c.send(type="approve", item_id=ids[0])
            await c.send(type="approve", item_id=ids[1])
            ended: set = set()
            while len(ended) < 3:
                e = await c.wait_for(lambda e: e.get("type") == "item_moved" and e.get("state") in END,
                                     timeout=120, approve=False)
                ended.add(e["item_id"])
            return ids
    ids = run(go())
    ships(factory, ids[0])
    ships(factory, ids[1])
    assert item(factory, ids[2])["state"] in ("scrapped", "held")


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
    assert not any(line.startswith("== Coder ") and ": Stage changes ==" in line for line in crate["log"]), \
        "it should skip the Coder's other steps"


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


def test_document_writer_writes_an_issue_without_a_planner(make_factory):
    """Straight from the intake to a Document writer set to GitHub issues: there's no plan, so
    Claude writes the issue from the crate's description, and it's created on its own (no
    tracking issue for a single one)."""
    line = {"id": "docs", "name": "Docs", "sources": ["game"], "storages": [], "repo": {"path": "test-project"},
            "intake": {"pos": [0, 8]}, "dock": {"pos": [9, 8]}, "scrap": {"pos": [9, 11]},
            "machines": [{"id": "issues", "type": "doc_writer", "pos": [5, 8], "options": {"doc_kind": "issues"}}],
            "belts": [{"from": "intake", "port": "out", "to": "issues"},
                      {"from": "issues", "port": "pass", "to": "dock"}]}
    build = simple_layout()
    build["sources"] = []
    factory = make_factory('\n[intake.github]\nrepo = "test/project"\nlabel = "factory"\n',
                           layout=[build, line])
    factory.start()

    async def go():
        async with factory.client() as c:
            n = await c.submit("Greet people when they arrive", pipeline="docs")
            await c.until_state(n, *END, timeout=60)
            return n
    n = run(go())
    ships(factory, n)
    import json
    issues = json.loads((factory.state / "issues.json").read_text())
    assert [i["title"] for i in issues] == ["Add a greeting"]
    assert "Part of" not in issues[0]["body"] and {"name": "factory"} in issues[0]["labels"]


def test_prd_plan_becomes_one_issue_and_one_crate_per_ticket(make_factory):
    """A PRD goes through the Planner and a Document writer set to "GitHub issues from a plan":
    a tracking issue plus one issue per ticket. The ready tickets come into the build line as a
    crate each; the one that waits for another is labelled, and comes in, once that one ships."""
    plan = {"id": "plan", "name": "Planning", "sources": ["game"], "storages": [], "repo": {"path": "test-project"},
            "intake": {"pos": [0, 8]}, "dock": {"pos": [9, 8]}, "scrap": {"pos": [9, 11]},
            "machines": [{"id": "planner", "type": "planner", "pos": [3, 8]},
                         {"id": "issues", "type": "doc_writer", "pos": [6, 8], "options": {"doc_kind": "issues"}}],
            "belts": [{"from": "intake", "port": "out", "to": "planner"},
                      {"from": "planner", "port": "pass", "to": "issues"},
                      {"from": "issues", "port": "pass", "to": "dock"}]}
    build = simple_layout()
    build["sources"] = []
    build["storages"] = [{"id": "store_1", "source": "github", "pos": [0, 3], "repo": "test/project"}]
    build["belts"].append({"from": "store_1", "port": "out", "to": "intake"})
    factory = make_factory('\n[intake.github]\nenabled = true\nrepo = "test/project"\nlabel = "factory"\n'
                           'poll_seconds = 1\n', layout=[build, plan])
    factory.start()

    async def go():
        async with factory.client() as c:
            prd = await c.submit("PRD: greet people, in two languages, and say goodbye", pipeline="plan")
            await c.until_state(prd, *END, timeout=120)
            seen: dict[str, int] = {}
            while len(seen) < 3:
                e = await c.wait_for(lambda e: e.get("type") == "item_created", timeout=120)
                seen[e["item"]["source_ref"]] = e["item"]["id"]
            for n in seen.values():
                if item(factory, n)["state"] not in END:
                    await c.until_state(n, *END, timeout=120)
            return prd, seen
    prd, seen = run(go())
    ships(factory, prd)
    import json
    issues = {i["number"]: i for i in json.loads((factory.state / "issues.json").read_text())}
    assert [i["title"] for i in issues.values()] == [
        "Plan: Greetings", "Add a greeting", "Translate the greeting", "Add a farewell"]
    # One crate per ticket, none for the tracking issue; the build line shipped each.
    assert sorted(seen) == ["102", "103", "104"]
    for n in seen.values():
        ships(factory, n)
    # T2 ("Translate", #103) waited for T1 (#102): it was only labelled, and came in, once #102
    # shipped, after the farewell (#104) that waited for nothing.
    assert seen["103"] > seen["104"] > seen["102"]
    assert "Task 2 of 3 in the plan #101. Waits for #102" in issues[103]["body"]
    assert "Task 1 of 3 in the plan #101. Ready now" in issues[102]["body"]
    assert {"name": "factory"} in issues[103]["labels"]
    # Every task shipped, so the plan's tracking issue was closed, once, with a comment. The
    # last ship's GitHub calls can land just after its crate does: give them a moment.
    import time
    gh_log = factory.state / "gh.log"
    for _ in range(100):
        log = gh_log.read_text(encoding="utf-8").splitlines()
        if any(l.startswith("issue close") for l in log):
            break
        time.sleep(0.1)
    assert [l for l in log if l.startswith("issue close")] == ["issue close 101 --repo test/project --reason completed"]
    assert any(l.startswith("issue comment 101 ") for l in log)


def test_sorter_sorts_by_whether_the_crate_passed_or_failed(make_factory):
    """The Tester's green and red belts both run into one sorter, which sends crates that
    failed at the Tester into a second bin and the ones that passed to the dock."""
    layout = simple_layout(machines=["coder", "tester"])
    layout["machines"].append({"id": "sort", "type": "sorter", "pos": [9, 0], "rules": [
        {"port": "out_3", "when": "result", "value": "fail:Tester"},
        {"port": "out_4", "when": "result", "value": "pass"}]})
    layout["bins"] = [{"id": "scrap_2", "pos": [12, 4]}]
    layout["dock"]["pos"] = [12, 0]
    layout["belts"] = [
        {"from": "intake", "port": "out", "to": "coder"},
        {"from": "coder", "port": "pass", "to": "tester"},
        {"from": "tester", "port": "pass", "to": "sort"},
        {"from": "tester", "port": "fail", "to": "sort"},
        {"from": "sort", "port": "out_3", "to": "scrap_2"},
        {"from": "sort", "port": "out_4", "to": "dock"},
    ]
    factory = make_factory(layout=[always_test(layout)])
    factory.start()

    async def go():
        async with factory.client() as c:
            assert not c.snapshot["layout"]["pipelines"][0].get("problem"), c.snapshot["layout"]["pipelines"][0]
            a = await c.submit("Passes")
            await c.until_state(a, *END, timeout=120)
            factory.flag("fail_tests")
            b = await c.submit("Fails its tests")
            await c.until_state(b, *END, timeout=120)
            return a, b
    a, b = run(go())
    ships(factory, a)
    assert "it passed" in " ".join(str(e) for e in item(factory, a)["log"])
    crate = item(factory, b)
    assert crate["state"] == "scrapped" and crate["location"] == "build:scrap_2", crate["log"][-10:]
    assert "it failed at Tester" in " ".join(str(e) for e in crate["log"])


def test_custom_storage_queues_changes(make_factory):
    """Changes typed into a Custom storage queue up (edit, remove and reorder them), survive a
    restart, and go onto the line one at a time, in order, from that storage."""
    layout = simple_layout()
    layout["sources"] = []
    layout["max_crates"] = 1
    layout["storages"] = [{"id": "store_1", "source": "custom", "pos": [0, 3]}]
    layout["belts"].append({"from": "store_1", "port": "out", "to": "intake"})
    factory = make_factory(layout=[layout])
    factory.flag("slow_coder", "1")
    factory.start()

    async def listing(c):
        await c.send(type="intake_list", pipeline="build")
        e = await c.wait_for(lambda e: e.get("type") == "intake_list")
        src = next(s for s in e["sources"] if s.get("storage") == "store_1")
        return [r for r in src["items"] if r.get("queue_id") is not None]

    async def fill():
        async with factory.client() as c:
            await c.send(type="pause", target="line", id="build", paused=True)
            await c.wait_for(lambda e: e.get("type") == "pauses")
            for k in range(4):
                await c.send(type="submit_work", text=f"Change {k}", pipeline="build", storage="store_1")
            q = await listing(c)
            assert [r["title"] for r in q] == ["Change 0", "Change 1", "Change 2", "Change 3"]
            await c.send(type="custom_queue_move", id=q[2]["queue_id"], delta=-2)  # Change 2 first
            await c.send(type="custom_queue_remove", id=q[1]["queue_id"])  # no Change 1
            await c.send(type="custom_queue_edit", id=q[3]["queue_id"], text="Change three")
            q = await listing(c)
            assert [r["title"] for r in q] == ["Change 2", "Change 0", "Change three"]
            assert q[0]["label"] == "Next"
    run(fill())
    factory.stop()
    factory.start()

    async def drain():
        async with factory.client() as c:
            assert [r["title"] for r in await listing(c)] == ["Change 2", "Change 0", "Change three"]
            await c.send(type="pause", target="line", id="build", paused=False)
            made = []
            while len(made) < 3:
                e = await c.wait_for(lambda e: e.get("type") == "item_created", timeout=180)
                made.append(e["item"])
                # one at a time: the line (max 1) takes the next only once this one has left it
                others = [m for m in made[:-1] if item(factory, m["id"])["state"] not in END]
                assert not others, others
            for m in made:
                if item(factory, m["id"])["state"] not in END:
                    await c.until_state(m["id"], *END, timeout=120)
            assert await listing(c) == []
            return made
    made = run(drain())
    assert [m["text"] for m in made] == ["Change 2", "Change 0", "Change three"]
    for m in made:
        assert item(factory, m["id"])["storage"] == "store_1"
        ships(factory, m["id"])


def test_storage_switched_off(make_factory):
    """A storage switched off from its window is saved off, isn't checked, and brings nothing in
    (a Custom one's queue waits) until it's switched on again."""
    layout = simple_layout()
    layout["sources"] = []
    layout["storages"] = [{"id": "store_1", "source": "custom", "pos": [0, 3]}]
    layout["belts"].append({"from": "store_1", "port": "out", "to": "intake"})
    factory = make_factory(layout=[layout])
    factory.start()

    async def go():
        async with factory.client() as c:
            await c.send(type="storage_enabled", pipeline="build", storage="store_1", enabled=False)
            await c.wait_for(lambda e: e.get("type") == "snapshot" and any(
                st.get("enabled") is False for p in e.get("layout", {}).get("pipelines", []) for st in p.get("storages", [])))
            saved = json.loads((factory.backend / "pipelines.json").read_text(encoding="utf-8"))
            assert saved["pipelines"][0]["storages"][0]["enabled"] is False
            await c.send(type="submit_work", text="Waits", pipeline="build", storage="store_1")
            await c.send(type="intake_list", pipeline="build")
            e = await c.wait_for(lambda e: e.get("type") == "intake_list")
            src = next(s for s in e["sources"] if s.get("storage") == "store_1")
            assert src["disabled"] is True
            assert [r["title"] for r in src["items"] if r.get("queue_id") is not None] == ["Waits"]
            with pytest.raises(AssertionError):  # the queue would have let it go by now
                await c.wait_for(lambda e: e.get("type") == "item_created", timeout=5)
            await c.send(type="storage_enabled", pipeline="build", storage="store_1", enabled=True)
            made = await c.wait_for(lambda e: e.get("type") == "item_created", timeout=30)
            assert made["item"]["text"] == "Waits"
            saved = json.loads((factory.backend / "pipelines.json").read_text(encoding="utf-8"))
            assert "enabled" not in saved["pipelines"][0]["storages"][0]
    run(go())
