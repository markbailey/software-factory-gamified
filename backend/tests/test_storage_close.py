"""Closing issues and PRs from a storage's window (intake_close): GitHub issues, open PRs, PRs to
review and Jira issues, as the storage's account, and the closed ones leave the listing."""
from __future__ import annotations

import asyncio
import json

import pytest

from factory import intake
from factory.config import GitHubIntakeConfig, JiraIntakeConfig
from factory.store import Store


class _Factory:
    def __init__(self, store):
        self.store = store
        self.lines = {}

    def _saved_repos(self):
        return []

    def line_for_source(self, source):
        return None


@pytest.fixture
def factory(tmp_path):
    return _Factory(Store(tmp_path / "f.db"))


def _github(cls, factory, calls, listing):
    cfg = GitHubIntakeConfig(repo="o/r") if cls is not intake.ReviewRequestIntake else None
    if cls is intake.ReviewRequestIntake:
        adapter = cls(intake.ReviewIntakeConfig(enabled=True), factory)
    else:
        adapter = cls(cfg, factory, pipeline="p")

    def gh(*args, stdin=None):
        calls.append(list(args))
        if args[:2] in (("issue", "list"), ("pr", "list"), ("search", "prs")):
            return json.dumps(listing)
        return ""
    adapter._gh = gh
    return adapter


def test_github_issue_close_with_comment_and_reason_then_hidden(factory):
    calls = []
    adapter = _github(intake.GitHubIntake, factory, calls, [{"number": 7, "title": "t", "url": "u"}])
    feed = intake.Feed(adapter, 60)
    out = asyncio.run(feed.close(["7"], "no longer needed", "not planned"))
    assert out == {"closed": ["7"], "failed": []}
    assert ["issue", "close", "7", "--repo", "o/r", "--reason", "not planned", "--comment", "no longer needed"] in calls
    listing = asyncio.run(feed.listing())
    assert listing["items"] == [] and listing["can_close"] is True


def test_open_pr_close_keeps_branch(factory):
    calls = []
    adapter = _github(intake.GitHubPrIntake, factory, calls, [])
    asyncio.run(intake.Feed(adapter, 60).close(["3"], ""))
    assert ["pr", "close", "3", "--repo", "o/r"] in calls
    assert not any("--delete-branch" in c for c in calls)


def test_review_pr_close_by_url(factory):
    calls = []
    url = "https://github.com/x/y/pull/9"
    adapter = _github(intake.ReviewRequestIntake, factory, calls,
                      [{"url": url, "number": 9, "title": "t", "repository": {"nameWithOwner": "x/y"}}])
    feed = intake.Feed(adapter, 60)
    out = asyncio.run(feed.close([url], "superseded"))
    assert out["closed"] == [url]
    assert ["pr", "close", url, "--comment", "superseded"] in calls
    assert asyncio.run(feed.listing())["items"] == []


def test_failure_is_reported_per_ref(factory):
    adapter = _github(intake.GitHubIntake, factory, [], [])

    def gh(*args, stdin=None):
        if args[2] == "8":
            raise RuntimeError("no permission")
        return ""
    adapter._gh = gh
    out = asyncio.run(intake.Feed(adapter, 60).close(["7", "8"]))
    assert out["closed"] == ["7"] and out["failed"] == [{"ref": "8", "error": "no permission"}]


TRANSITIONS = {"transitions": [
    {"id": "11", "name": "Start", "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}},
    {"id": "31", "name": "Won't do", "to": {"name": "Rejected", "statusCategory": {"key": "done"}}},
    {"id": "41", "name": "Close", "to": {"name": "Done", "statusCategory": {"key": "done"}}},
]}


def _jira(factory, calls, issues):
    adapter = intake.JiraIntake(JiraIntakeConfig(jql="project = X"), factory, pipeline="p")

    def request(method, path, payload=None):
        calls.append((method, path, payload))
        if path.endswith("/transitions") and method == "GET":
            return TRANSITIONS
        if path.endswith("/search/jql"):
            return {"issues": issues}
        return {}
    adapter._request = request
    adapter._site = lambda: "https://x.atlassian.net"
    return adapter


def test_jira_close_picks_done_transition_and_comments(factory):
    calls = []
    adapter = _jira(factory, calls, [])
    feed = intake.Feed(adapter, 60)
    opts = asyncio.run(feed.close_options(["X-1"]))
    assert opts["default"] == "Close" and [o["done"] for o in opts["transitions"]] == [True, True, False]
    out = asyncio.run(feed.close(["X-1"], "done elsewhere"))
    assert out["closed"] == ["X-1"]
    assert ("POST", "/rest/api/3/issue/X-1/transitions", {"transition": {"id": "41"}}) in calls
    assert any(c[1] == "/rest/api/3/issue/X-1/comment" for c in calls)


def test_jira_close_with_chosen_transition_and_unknown_one(factory):
    calls = []
    feed = intake.Feed(_jira(factory, calls, []), 60)
    asyncio.run(feed.close(["X-1"], "", transition="Won't do"))
    assert ("POST", "/rest/api/3/issue/X-1/transitions", {"transition": {"id": "31"}}) in calls
    out = asyncio.run(feed.close(["X-2"], "", transition="Nope"))
    assert out["closed"] == [] and "can't take 'Nope'" in out["failed"][0]["error"]


def test_jira_closed_issue_stays_out_of_listing_and_poll_while_done(factory):
    done = {"key": "X-1", "fields": {"summary": "s", "status": {"name": "Done", "statusCategory": {"key": "done"}}}}
    adapter = _jira(factory, [], [done])
    feed = intake.Feed(adapter, 60)
    asyncio.run(feed.close(["X-1"]))
    adapter.closed_refs = {}  # as after a restart
    assert asyncio.run(feed.listing())["items"] == []
    reopened = {"key": "X-1", "fields": {"summary": "s", "status": {"name": "To Do", "statusCategory": {"key": "new"}}}}
    adapter2 = _jira(factory, [], [reopened])
    assert [r["ref"] for r in asyncio.run(intake.Feed(adapter2, 60).listing())["items"]] == ["X-1"]
