"""An Open PRs storage ticked "Only repos in the factory's repo list" reads every repo added under
the gear instead of its one repo, needs no repo of its own, and refs are owner/name#number."""
from __future__ import annotations

import asyncio
import json

import pytest

from factory import graph, intake
from factory.config import GitHubIntakeConfig
from factory.store import Store


class _Factory:
    def __init__(self, store, repos):
        self.store = store
        self.lines = {}
        self.repos = repos
        self.submitted = []

    def _saved_repos(self):
        return list(self.repos)

    async def submit(self, text, **kw):
        self.submitted.append(kw)
        return {"id": len(self.submitted)}


@pytest.fixture
def factory(tmp_path):
    return _Factory(Store(tmp_path / "f.db"), ["a/one", "b/two"])


def _adapter(factory, calls):
    adapter = intake.ListedReposPrIntake(GitHubIntakeConfig(repo=intake.LISTED_REPOS), factory, pipeline="p")

    def gh(*args, stdin=None):
        calls.append(list(args))
        if args[:2] == ("pr", "list"):
            repo = args[args.index("--repo") + 1]
            return json.dumps([{"number": 1, "title": f"in {repo}", "body": "", "url": f"https://github.com/{repo}/pull/1",
                                "headRefOid": "abc", "labels": [], "author": {"login": "me"}}])
        return ""
    adapter._gh = gh
    return adapter


def test_polls_every_listed_repo_once(factory):
    calls = []
    adapter = _adapter(factory, calls)
    assert asyncio.run(adapter.poll()) == 2
    assert [s["repo"] for s in factory.submitted] == ["a/one", "b/two"]
    assert asyncio.run(adapter.poll()) == 0  # each PR is taken once


def test_listing_and_close_use_repo_refs(factory):
    calls = []
    adapter = _adapter(factory, calls)
    rows = asyncio.run(adapter.listing())
    assert [r["ref"] for r in rows] == ["a/one#1", "b/two#1"]
    out = asyncio.run(intake.Feed(adapter, 60).close(["b/two#1", "c/three#1"]))
    assert out["closed"] == ["b/two#1"] and out["failed"][0]["ref"] == "c/three#1"
    assert ["pr", "close", "1", "--repo", "b/two"] in calls
    assert [r["ref"] for r in asyncio.run(adapter.listing())] == ["a/one#1"]


def test_empty_list_brings_nothing(factory):
    factory.repos = []
    adapter = _adapter(factory, [])
    assert asyncio.run(adapter.poll()) == 0
    assert "none are listed" in adapter.describe()


def test_ticked_storage_needs_no_repo():
    st = {"id": "store_1", "source": "github_pr", "pos": [0, 0], "listed_repos_only": True}
    p = {"id": "x", "sources": [], "storages": [st], "belts": [{"from": "store_1", "port": "out", "to": "intake"}]}
    assert graph.lists_repos(st)
    assert not graph.missing_repo(p)
    st.pop("listed_repos_only")
    assert not graph.lists_repos(st) and graph.missing_repo(p)
