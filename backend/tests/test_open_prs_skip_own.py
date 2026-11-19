"""An Open PRs storage leaves alone the PRs the factory opened itself while their crate is still
on a line (found by the crate's saved PR link or branch name), unless it's ticked to include them."""
from __future__ import annotations

import asyncio
import json

import pytest

from factory import graph, intake
from factory.config import GitHubIntakeConfig
from factory.store import Store

REPO = "me/app"


class _Factory:
    def __init__(self, store):
        self.store = store
        self.lines = {}
        self.submitted = []

    async def submit(self, text, **kw):
        self.submitted.append(kw)
        return self.store.create_item(text, source=kw.get("source", ""), source_ref=kw.get("source_ref", ""),
                                   pr_url=kw.get("pr_url", ""), pipeline=kw.get("pipeline", ""))


def _pr(n, branch):
    return {"number": n, "title": f"PR {n}", "body": "", "url": f"https://github.com/{REPO}/pull/{n}",
            "headRefOid": "abc", "headRefName": branch, "labels": [], "author": {"login": "me"}}


PRS = [_pr(55, "feature/thing"), _pr(56, "crate/7"), _pr(57, "fix/login-33"), _pr(58, "someone/else")]


@pytest.fixture
def factory(tmp_path):
    f = _Factory(Store(tmp_path / "f.db"))
    # Crate A opened PR #55 (saved PR link); crate B is on branch fix/login-33 (saved name), PR not saved yet.
    a = f.store.create_item("a", pr_url=f"https://github.com/{REPO}/pull/55", pipeline="build")
    b = f.store.create_item("b", pipeline="build")
    f.store.update_item(b["id"], branch="fix/login-33")
    f.ids = (a["id"], b["id"])
    return f


def _adapter(factory, include=False):
    adapter = intake.GitHubPrIntake(GitHubIntakeConfig(repo=REPO), factory, pipeline="p", include_factory_prs=include)
    adapter._gh = lambda *args, stdin=None: json.dumps(PRS) if args[:2] == ("pr", "list") else ""
    return adapter


def test_skips_prs_of_live_crates(factory):
    asyncio.run(_adapter(factory).poll())
    # #56 is crate/7, and there's no crate 7: it's taken like anyone's PR.
    assert sorted(s["source_ref"] for s in factory.submitted) == ["56", "58"]
    rows = {r["ref"]: r for r in asyncio.run(_adapter(factory).listing())}
    assert rows["55"]["factory_crate"] == factory.ids[0] and not rows["55"]["auto"]
    assert rows["57"]["factory_crate"] == factory.ids[1]
    assert "factory_crate" not in rows["58"] and rows["58"]["auto"]


def test_taken_once_its_crate_is_done(factory):
    asyncio.run(_adapter(factory).poll())
    factory.store.update_item(factory.ids[0], state="shipped")  # its PR is still open: now it's checked
    asyncio.run(_adapter(factory).poll())
    assert "55" in [s["source_ref"] for s in factory.submitted]
    assert "57" not in [s["source_ref"] for s in factory.submitted]


def test_legacy_crate_branch_and_other_repo(factory):
    c = factory.store.create_item("c", pipeline="build")
    PRS.append(_pr(59, f"crate/{c['id']}"))
    try:
        asyncio.run(_adapter(factory).poll())
        assert "59" not in [s["source_ref"] for s in factory.submitted]
        factory.store.update_item(c["id"], repo="other/repo")  # same branch name, another repo: not its
        asyncio.run(_adapter(factory).poll())
        assert "59" in [s["source_ref"] for s in factory.submitted]
    finally:
        PRS.pop()


def test_tick_includes_them(factory):
    asyncio.run(_adapter(factory, include=True).poll())
    assert sorted(s["source_ref"] for s in factory.submitted) == ["55", "56", "57", "58"]
    assert graph.includes_factory_prs({"source": "github_pr", "include_factory_prs": True})
    assert not graph.includes_factory_prs({"source": "github_pr"})
