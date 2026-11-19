"""Shipped crates' PRs are checked again until merged or closed, in one batched GitHub call."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from factory import engine as engine_mod
from factory.engine import Factory as Engine


class FakeStore:
    def __init__(self, items):
        self.items = {i["id"]: dict(i) for i in items}

    def list_items(self, log_tail=None):
        return [dict(self.items[k]) for k in sorted(self.items)]

    def get_item(self, item_id):
        return self.items.get(item_id)

    def update_item(self, item_id, **fields):
        self.items[item_id].update(fields)


class FakeHub:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


URL_A = "https://github.com/o/r/pull/1"
URL_B = "https://github.com/o/r/pull/2"


def engine(items, answers=None, fail=False):
    """answers: url -> state the fake GitHub gives. Records every batch asked for."""
    e = Engine.__new__(Engine)
    e.store, e.hub = FakeStore(items), FakeHub()
    e.asked = []

    def batch(urls):
        e.asked.append(list(urls))
        if fail:
            raise RuntimeError("gh: network down")
        return {u: answers[u] for u in urls if u in (answers or {})}

    e._pr_states_batch = batch
    return e


def shipped(n, url=URL_A, pr_state="open", state="shipped"):
    return {"id": n, "state": state, "pr_url": url, "pr_state": pr_state}


def test_open_to_merged_updates_store_and_emits():
    e = engine([shipped(1)], {URL_A: "merged"})
    assert asyncio.run(e.refresh_pr_states(force=True)) == 1
    assert e.store.items[1]["pr_state"] == "merged"
    assert e.hub.events == [{"type": "pr_state", "item_id": 1, "url": URL_A, "state": "merged"}]


def test_settled_and_live_crates_are_not_asked_about():
    e = engine([shipped(1, URL_A, "merged"), shipped(2, URL_B, "CLOSED"),
                shipped(3, "https://github.com/o/r/pull/3", "", state="running"),
                shipped(4, "https://github.com/o/r/pull/4", "", state="awaiting_approval")], {})
    assert asyncio.run(e.refresh_pr_states(force=True)) == 0
    assert e.asked == []


def test_two_crates_sharing_a_pr_both_update():
    e = engine([shipped(1), shipped(2, pr_state="unknown")], {URL_A: "closed"})
    assert asyncio.run(e.refresh_pr_states(force=True)) == 2
    assert e.asked == [[URL_A]]
    assert e.store.items[1]["pr_state"] == e.store.items[2]["pr_state"] == "closed"


def test_unchanged_state_emits_nothing():
    e = engine([shipped(1)], {URL_A: "open"})
    assert asyncio.run(e.refresh_pr_states(force=True)) == 0
    assert e.hub.events == []


def test_throttled_unless_forced():
    e = engine([shipped(1)], {URL_A: "open"})

    async def run():
        await e.refresh_pr_states()
        await e.refresh_pr_states()  # within PR_REFRESH_MIN_GAP: skipped
        await e.refresh_pr_states(force=True)

    asyncio.run(run())
    assert len(e.asked) == 2


def test_gh_failure_leaves_states_alone():
    e = engine([shipped(1)], fail=True)
    assert asyncio.run(e.refresh_pr_states(force=True)) == 0
    assert e.store.items[1]["pr_state"] == "open" and e.hub.events == []


def test_batches_of_pr_batch(monkeypatch):
    monkeypatch.setattr(engine_mod, "PR_BATCH", 2)
    urls = [f"https://github.com/o/r/pull/{n}" for n in range(5)]
    e = engine([shipped(n + 1, u) for n, u in enumerate(urls)], {u: "merged" for u in urls})
    assert asyncio.run(e.refresh_pr_states(force=True)) == 5
    assert [len(b) for b in e.asked] == [2, 2, 1]


# _pr_states_batch -------------------------------------------------------------------------

def batch_engine(monkeypatch, stdout, returncode=0, stderr=""):
    e = Engine.__new__(Engine)
    e.cfg = SimpleNamespace(allowed_commands=["gh"], workspace=".")
    calls = []
    monkeypatch.setattr(engine_mod.runner, "resolve", lambda argv, allowed: argv)

    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(engine_mod.subprocess, "run", run)
    return e, calls


def test_batch_parses_graphql(monkeypatch):
    out = {"data": {"p0": {"pullRequest": {"state": "MERGED"}}, "p2": {"pullRequest": {"state": "OPEN"}}}}
    e, calls = batch_engine(monkeypatch, json.dumps(out))
    got = e._pr_states_batch([URL_A, "https://gitlab.com/o/r/merge_requests/5", "https://github.com/x/y/pull/9"])
    assert got == {URL_A: "merged", "https://github.com/x/y/pull/9": "open"}
    query = calls[0][-1]
    assert calls[0][:3] == ["gh", "api", "graphql"]
    assert 'p0: repository(owner: "o", name: "r") { pullRequest(number: 1)' in query
    assert "gitlab" not in query and "p1:" not in query


def test_batch_keeps_partial_data_with_errors(monkeypatch):
    out = {"data": {"p0": {"pullRequest": {"state": "CLOSED"}}, "p1": None},
           "errors": [{"message": "Could not resolve to a Repository"}]}
    e, _ = batch_engine(monkeypatch, json.dumps(out), returncode=1)
    assert e._pr_states_batch([URL_A, "https://github.com/gone/repo/pull/3"]) == {URL_A: "closed"}


def test_batch_raises_when_gh_fails_with_nothing(monkeypatch):
    e, _ = batch_engine(monkeypatch, "", returncode=1, stderr="HTTP 401")
    with pytest.raises(RuntimeError, match="401"):
        e._pr_states_batch([URL_A])


def test_batch_skips_gh_with_no_github_urls(monkeypatch):
    e, calls = batch_engine(monkeypatch, "{}")
    assert e._pr_states_batch(["", "https://example.com/pr/1"]) == {}
    assert calls == []
