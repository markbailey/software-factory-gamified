"""Claude cost per crate and machine, the budget caps, stats, alerts and lazy logs."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from .conftest import item, simple_layout
from .test_factory import always_test

FAKE_COST = 0.01  # what tests/fakes/fake_claude.py says each Claude call cost


def run(coro):
    return asyncio.run(coro)


def test_cost_and_stats_after_a_crate_ships(factory):
    factory.start()

    async def go():
        async with factory.client() as c:
            n = await c.submit("Count my cost")
            await c.until_state(n, "shipped", "held", "scrapped")
            cost = await c.wait_for(lambda e: e.get("type") == "item_cost" and e.get("item_id") == n, timeout=5) \
                if not any(e.get("type") == "item_cost" for e in c.events) else \
                [e for e in c.events if e.get("type") == "item_cost"][-1]
            await c.send(type="stats", days=7)
            stats = await c.wait_for(lambda e: e.get("type") == "stats")
            return n, cost, stats
    n, cost, stats = run(go())
    assert item(factory, n)["state"] == "shipped"
    assert cost["cost_usd"] >= FAKE_COST
    assert stats["shipped"] == 1 and stats["scrapped"] == 0
    assert stats["cost_usd"] >= FAKE_COST and stats["per_day"][-1]["shipped"] == 1
    coder = next(m for m in stats["machines"] if m["id"] == "coder")
    assert coder["runs"] == 1 and coder["failed"] == 0 and coder["cost_usd"] >= FAKE_COST
    assert {m["id"] for m in stats["machines"]} >= {"coder", "tester", "packager"}


def test_snapshot_sends_the_end_of_logs_and_get_log_the_rest(factory):
    factory.flag("chatty", "100")
    factory.start()

    async def go():
        async with factory.client() as c:
            n = await c.submit("A long log")
            await c.until_state(n, "shipped", "held", "scrapped")
        async with factory.client() as c:
            snap = next(i for i in c.snapshot["items"] if i["id"] == n)
            await c.send(type="get_log", item_id=n)
            full = await c.wait_for(lambda e: e.get("type") == "item_log")
            return snap, full
    snap, full = run(go())
    assert snap["log_lines"] == len(full["log"]) > len(snap["log"])
    assert snap["log"] == full["log"][-len(snap["log"]):]
    assert snap["cost_usd"] >= FAKE_COST


def test_crate_over_its_budget_is_held_and_run_again_carries_on(make_factory):
    layout = simple_layout(["refiner", "coder", "tester", "packager"])  # two Claude machines
    factory = make_factory("\n[budget]\ncrate_usd = 0.005\n", [layout])
    factory.start()

    async def go():
        async with factory.client() as c:
            n = await c.submit("Expensive")
            held = await c.wait_for(lambda e: e.get("type") == "item_held" and e.get("item_id") == n, timeout=60)
            assert "budget" in held["reason"]
            await c.send(type="retry", item_id=n)
            await c.until_state(n, "shipped", "scrapped", timeout=90)
            return n
    n = run(go())
    assert item(factory, n)["state"] == "shipped"


def test_daily_budget_makes_crates_wait(make_factory):
    factory = make_factory("\n[budget]\ndaily_usd = 0.005\n")
    factory.start()

    async def go():
        async with factory.client() as c:
            a = await c.submit("First, spends the budget")
            await c.wait_for(lambda e: e.get("type") == "item_cost" and e.get("item_id") == a)
            b = await c.submit("Second, waits")
            waiting = await c.wait_for(lambda e: e.get("type") == "item_waiting" and e.get("item_id") == b
                                       and "budget" in e.get("reason", ""), timeout=30)
            await c.wait_for(lambda e: e.get("type") == "budget_spent", timeout=5) \
                if not any(e.get("type") == "budget_spent" for e in c.events) else None
            return b
    b = run(go())
    assert item(factory, b)["state"] == "waiting"
    assert not [r for r in json.dumps(item(factory, b)["log"]).split("==") if "Coder 4/" in r]


class _Hook(BaseHTTPRequestHandler):
    got: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        _Hook.got.append(json.loads(body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def test_alerts_go_to_a_webhook(make_factory):
    _Hook.got = []
    server = HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/hook"
    factory = make_factory(f'\n[alerts]\nwebhook = "{url}"\non = ["held", "shipped"]\n',
                           layout=[always_test(simple_layout())])
    factory.flag("fail_tests")
    factory.start()

    async def go():
        async with factory.client() as c:
            n = await c.submit("Alert me")
            await c.until_state(n, "held", timeout=90)
            factory.unflag("fail_tests")
            await c.send(type="retry", item_id=n)
            await c.until_state(n, "shipped", "scrapped", timeout=90)
            await asyncio.sleep(1)
            return n
    try:
        n = run(go())
    finally:
        server.shutdown()
    kinds = [g["kind"] for g in _Hook.got]
    assert kinds == ["held", "shipped"], _Hook.got
    assert f"Crate #{n}" in _Hook.got[0]["text"]
