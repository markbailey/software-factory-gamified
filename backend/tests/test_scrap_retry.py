"""A scrap bin that hands issues back for retry: it comments why the last try failed, leaves the
issue labelled for the factory so the intake picks it up again (with that reason in the spec),
and gives up after max_retries so a crate can't loop forever."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from factory import engine, intake
from factory.config import GitHubIntakeConfig, JiraIntakeConfig
from factory.store import Store

WHY = "Coder: work failed\n\nThe tests in test_login.py don't compile: missing import."


class _Factory:
    def __init__(self, store, bin_raw, workspace=None):
        self.store = store
        self.cfg = SimpleNamespace(workspace=workspace or Path("no-such-workspace"))
        self.lines = {}
        self.bin_raw = bin_raw
        self.submitted = []

    def bin_rules(self, location):
        return engine.bin_rules(self.bin_raw)

    def rollback_labels(self):
        return set()

    async def submit(self, text, **kw):
        self.submitted.append(text)
        item = self.store.create_item(text, source=kw.get("source", ""), source_ref=kw.get("source_ref", ""))
        return item


def _gh_adapter(factory, calls, issues):
    a = intake.GitHubIntake(GitHubIntakeConfig(repo="o/r"), factory, pipeline="p")

    def gh(*args, stdin=None):
        calls.append((list(args), stdin))
        if args[:2] == ("issue", "list"):
            return json.dumps(issues)
        if args[:2] == ("issue", "view"):
            return json.dumps({"labels": []})
        return ""
    a._gh = gh
    return a


def _scrapped(factory, ref, source="github"):
    item = factory.store.create_item("t", source=source, source_ref=ref, pipeline="p")
    factory.store.update_item(item["id"], state="scrapped", location="p:scrap", scrap_kind="work_failed")
    return factory.store.get_item(item["id"])


def _comments(calls):
    return [stdin for args, stdin in calls if args[:2] == ["issue", "comment"]]


def test_bin_rules_default_off_and_clamped():
    assert engine.bin_rules(None)["retry_issues"] is False
    assert engine.bin_rules(None)["max_retries"] == 2
    assert engine.bin_rules({"max_retries": 99})["max_retries"] == 10
    assert engine.bin_rules({"max_retries": "x"})["max_retries"] == 2
    assert engine._bin_extras({"retry_issues": True, "max_retries": 3})["retry_issues"] is True


def test_github_retry_keeps_label_comments_and_is_picked_up_with_reason(tmp_path):
    f = _Factory(Store(tmp_path / "f.db"), {"close_issues": False, "retry_issues": True, "max_retries": 2})
    calls = []
    issue = {"number": 7, "title": "Fix login", "body": "b", "url": "u"}
    a = _gh_adapter(f, calls, [issue])
    asyncio.run(a.poll())
    assert len(f.submitted) == 1 and "Retry" not in f.submitted[0]
    asyncio.run(a.notify(_scrapped(f, "7"), "scrapped", WHY))
    edits = [args for args, _ in calls if args[:2] == ["issue", "edit"]]
    assert not any("--remove-label" in e and a.cfg.label in e for e in edits)  # still labelled
    assert any(e[-2:] == ["--add-label", a.cfg.label] for e in edits)
    assert not any(e[-2:] == ["--add-label", "work failed"] for e in edits)
    body = _comments(calls)[-1]
    assert "missing import" in body and "retry 1 of 2" in body
    asyncio.run(a.poll())  # the intake takes it again, with why it failed last time
    assert len(f.submitted) == 2
    assert "Retry 1" in f.submitted[1] and "missing import" in f.submitted[1]


def test_github_gives_up_after_max_retries(tmp_path):
    f = _Factory(Store(tmp_path / "f.db"), {"close_issues": False, "retry_issues": True, "max_retries": 1})
    calls = []
    a = _gh_adapter(f, calls, [{"number": 7, "title": "t", "body": "", "url": "u"}])
    asyncio.run(a.poll())
    asyncio.run(a.notify(_scrapped(f, "7"), "scrapped", WHY))  # retry 1 of 1
    asyncio.run(a.poll())
    calls.clear()
    asyncio.run(a.notify(_scrapped(f, "7"), "scrapped", WHY))  # out of tries: the usual hand-back
    edits = [args for args, _ in calls if args[:2] == ["issue", "edit"]]
    assert ["issue", "edit", "7", "--repo", "o/r", "--remove-label", a.cfg.label] in edits
    assert "tried this 2 times" in _comments(calls)[-1]
    assert f.store.retries("github", "7") == (0, "")  # labelled again by hand, it gets fresh tries


def test_github_retry_off_is_the_usual_hand_back(tmp_path):
    f = _Factory(Store(tmp_path / "f.db"), {"close_issues": False})
    calls = []
    a = _gh_adapter(f, calls, [])
    asyncio.run(a.notify(_scrapped(f, "7"), "scrapped", WHY))
    edits = [args for args, _ in calls if args[:2] == ["issue", "edit"]]
    assert ["issue", "edit", "7", "--repo", "o/r", "--remove-label", a.cfg.label] in edits
    assert "missing import" in _comments(calls)[-1]  # the hand-back always said why


def test_closing_bin_ignores_retry(tmp_path):
    f = _Factory(Store(tmp_path / "f.db"), {"close_issues": True, "retry_issues": True})
    calls = []
    a = _gh_adapter(f, calls, [])
    asyncio.run(a.notify(_scrapped(f, "7"), "scrapped", WHY))
    assert any(args[:2] == ["issue", "close"] for args, _ in calls)


def test_jira_retry_keeps_trigger_label_and_goes_to_todo(tmp_path):
    f = _Factory(Store(tmp_path / "f.db"), {"close_issues": False, "retry_issues": True, "max_retries": 2})
    a = intake.JiraIntake(JiraIntakeConfig(jql="labels = factory", trigger_label="factory"), f, pipeline="p")
    calls = []
    issues = [{"key": "X-1", "fields": {"summary": "s", "status": {"name": "To Do"}}}]

    def request(method, path, payload=None):
        calls.append((method, path, payload))
        if path.endswith("/transitions") and method == "GET":
            return {"transitions": [{"id": "5", "name": "To Do", "to": {"name": "To Do"}}]}
        if path.endswith("/search/jql"):
            return {"issues": issues}
        return {}
    a._request = request
    a._site = lambda: "https://x.atlassian.net"
    asyncio.run(a.poll())
    item = _scrapped(f, "X-1", source="jira")
    f.store.update_item(item["id"], pipeline="p")
    asyncio.run(a.notify(f.store.get_item(item["id"]), "scrapped", WHY))
    labels = [c[2] for c in calls if c[1] == "/rest/api/3/issue/X-1" and c[0] == "PUT"]
    assert labels == [{"update": {"labels": [{"add": "factory"}]}}]
    assert ("POST", "/rest/api/3/issue/X-1/transitions", {"transition": {"id": "5"}}) in calls
    comment = next(c[2] for c in calls if c[1] == "/rest/api/3/issue/X-1/comment")
    assert "missing import" in json.dumps(comment)
    asyncio.run(a.poll())
    assert len(f.submitted) == 2 and "Retry 1" in f.submitted[1]
