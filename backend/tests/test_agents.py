"""Choosing the Claude model and effort per machine (and the factory-wide default under the gear)."""
import asyncio
import json
import sys

from factory import agents, config
from factory.engine import _clean_pipeline

from .conftest import item, simple_layout


def test_build_line_keeps_a_claude_machines_choice_and_drops_bad_ones():
    catalog, _, _ = config.load_catalog()
    line = config.build_line(catalog, [
        {"type": "coder", "id": "coder", "model": "claude-haiku-5-5", "effort": "max"},
        {"type": "tester", "id": "tester", "model": "claude-opus-5-5", "effort": "high"},  # runs no Claude
        {"type": "reviewer", "id": "reviewer", "model": "rm -rf /", "effort": "huge"},
    ])
    assert (line[0].model, line[0].effort) == ("claude-haiku-5-5", "max")
    assert (line[1].model, line[1].effort) == ("", "")
    assert (line[2].model, line[2].effort) == ("", "")


def test_old_layouts_load_with_the_default():
    catalog, _, _ = config.load_catalog()
    m = config.build_line(catalog, [{"type": "coder", "id": "coder"}])[0]
    assert (m.model, m.effort) == ("", "")


def test_layout_save_keeps_model_and_effort():
    p = simple_layout()
    p["machines"][0].update(model="claude-sonnet-5-5", effort="low")
    saved = _clean_pipeline(p)
    assert saved["machines"][0]["model"] == "claude-sonnet-5-5"
    assert saved["machines"][0]["effort"] == "low"


def test_flags_go_after_the_executable_only_when_the_cli_has_them(monkeypatch):
    monkeypatch.setitem(agents._flags, "claude", {"--model"})
    argv, notes = agents.with_choice(["claude", "-p", "--verbose"], "claude-opus-5-5", "high")
    assert argv == ["claude", "--model", "claude-opus-5-5", "-p", "--verbose"]
    assert len(notes) == 1 and "--effort" in notes[0]
    assert agents.with_choice(["claude", "-p"], "", "") == (["claude", "-p"], [])
    # A step that names its own model keeps it.
    monkeypatch.setitem(agents._flags, "claude", {"--model", "--effort"})
    argv, _ = agents.with_choice(["claude", "-p", "--model", "x"], "claude-opus-5-5", "low")
    assert argv == ["claude", "--effort", "low", "-p", "--model", "x"]


def test_defaults_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "AGENTS_FILE", tmp_path / "agents.json")
    assert agents.defaults() == {"model": "", "effort": ""}
    agents.set_defaults("claude-fable-5-2", "XHIGH")
    assert agents.defaults() == {"model": "claude-fable-5-2", "effort": "xhigh"}


def test_machines_run_claude_with_their_choice_or_the_default(make_factory):
    layout = simple_layout(["refiner", "coder", "tester", "packager"])
    layout["machines"][1].update(model="claude-haiku-5-5", effort="low")
    factory = make_factory(layout=[layout])
    (factory.backend / "agents.json").write_text(json.dumps({"model": "claude-opus-5-5", "effort": "max"}))
    factory.start()

    async def go():
        async with factory.client() as c:
            n = await c.submit("Add a thing")
            await c.until_state(n, "shipped", "held", "scrapped", timeout=120)
            return n
    n = asyncio.run(go())
    assert item(factory, n)["state"] == "shipped", item(factory, n)["log"][-12:]
    runs = [json.loads(line) for line in (factory.state / "claude_args.jsonl").read_text().splitlines()]
    assert runs[0][:4] == ["--model", "claude-opus-5-5", "--effort", "max"]  # the Refiner: the default
    assert ["--model", "claude-haiku-5-5", "--effort", "low"] == runs[-1][:4]  # the Coder: its own


def test_used_choice_reads_the_flags_a_claude_command_runs_with():
    assert agents.used_choice(["claude", "--model", "claude-opus-5-5", "--effort", "high", "-p"]) == (
        "claude-opus-5-5", "high")
    assert agents.used_choice(["claude", "-p", "--model=claude-haiku-5-5"]) == ("claude-haiku-5-5", "default")
    assert agents.used_choice(["claude", "-p"]) == ("default", "default")


def test_old_runs_tables_get_model_and_effort_columns(tmp_path):
    import sqlite3

    from factory.store import Store
    db = tmp_path / "factory.db"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,"
                " pipeline TEXT NOT NULL DEFAULT '', machine TEXT NOT NULL, machine_type TEXT NOT NULL DEFAULT '',"
                " started_at REAL NOT NULL, ended_at REAL NOT NULL, outcome TEXT NOT NULL,"
                " failed_step TEXT NOT NULL DEFAULT '', cost_usd REAL NOT NULL DEFAULT 0,"
                " turns INTEGER NOT NULL DEFAULT 0)")
    old.execute("INSERT INTO runs (item_id, machine, started_at, ended_at, outcome, cost_usd)"
                " VALUES (1, 'coder', 1, 2, 'ok', 0.5)")
    old.commit()
    old.close()
    store = Store(db)
    store.record_run(1, "build", "coder", "coder", 3, "ok", cost_usd=0.25, model="claude-opus-5-5", effort="low")
    rows = store.runs_since(0)
    assert (rows[0]["model"], rows[0]["effort"]) == ("", "")
    assert (rows[1]["model"], rows[1]["effort"]) == ("claude-opus-5-5", "low")


def test_stats_by_model_and_effort():
    from factory.stats import by_agent

    def run(model, effort, cost, outcome="ok", secs=10.0):
        return {"model": model, "effort": effort, "cost_usd": cost, "outcome": outcome,
                "started_at": 0.0, "ended_at": secs, "turns": 2}
    rows = by_agent([
        run("claude-opus-5-5", "high", 1.0), run("claude-opus-5-5", "high", 0.5, "failed", 30.0),
        run("claude-haiku-5-5", "low", 0.1), run("claude-haiku-5-5", "low", 0.2, "stopped"),
        run("", "", 0.0),  # a tester: no Claude, left out
        run("", "", 0.05),  # from before models were recorded
    ])
    assert [(r["model"], r["effort"]) for r in rows] == [
        ("claude-opus-5-5", "high"), ("claude-haiku-5-5", "low"), ("unknown", "unknown")]
    opus, haiku, unknown = rows
    assert (opus["runs"], opus["failed"], opus["cost_usd"], opus["seconds"], opus["avg_seconds"]) == (
        2, 1, 1.5, 40.0, 20.0)
    assert (haiku["runs"], haiku["cost_usd"]) == (1, 0.3)  # a stopped run still cost something
    assert (unknown["runs"], unknown["cost_usd"]) == (1, 0.05)


def test_stats_show_the_model_and_effort_each_run_used(make_factory):
    layout = simple_layout(["refiner", "coder", "tester", "packager"])
    layout["machines"][1].update(model="claude-haiku-5-5", effort="low")
    factory = make_factory(layout=[layout])
    (factory.backend / "agents.json").write_text(json.dumps({"model": "claude-opus-5-5", "effort": "max"}))
    factory.start()

    async def go():
        async with factory.client() as c:
            n = await c.submit("Add a thing")
            await c.until_state(n, "shipped", "held", "scrapped", timeout=120)
            await c.send(type="stats", days=7)
            return n, await c.wait_for(lambda e: e.get("type") == "stats")
    n, stats = asyncio.run(go())
    assert item(factory, n)["state"] == "shipped", item(factory, n)["log"][-12:]
    picks = {(a["model"], a["effort"]): a for a in stats["agents"]}
    assert ("claude-haiku-5-5", "low") in picks and ("claude-opus-5-5", "max") in picks
    assert all(a["runs"] >= 1 for a in picks.values())
    assert abs(sum(a["cost_usd"] for a in stats["agents"]) - stats["cost_usd"]) < 0.001
