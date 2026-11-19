"""A PR that fails at the Merger with merge conflicts goes back to the Coder, which must fix that
same PR (its own branch, with the base merged in), not open a new one; and a crate can only go
round the red belt max_rework times before it's scrapped."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from factory import branch_names, intake
from factory.config import GitHubIntakeConfig
from factory.engine import Factory as Engine
from factory.store import Store

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


# --- Open PRs intake: the crate works on the PR's branch ------------------------------------

class _Factory:
    def __init__(self, store):
        self.store = store
        self.lines = {}

    async def submit(self, text, **kw):
        return self.store.create_item(text, kw["source"], kw["source_ref"], kw["source_url"],
                                      pipeline="p", pr_url=kw["pr_url"])


def _adapter(store, prs):
    a = intake.GitHubPrIntake(GitHubIntakeConfig(repo="o/r"), _Factory(store), pipeline="p")
    a._gh = lambda *args, stdin=None: json.dumps(prs) if args[:2] == ("pr", "list") else ""
    return a


def _pr(sha="aaa", head="51_add-bars", cross=False):
    return {"number": 65, "title": "Add bars", "body": "", "url": "https://github.com/o/r/pull/65",
            "headRefOid": sha, "headRefName": head, "isCrossRepository": cross}


def test_a_pr_crate_keeps_the_prs_branch(tmp_path):
    store = Store(tmp_path / "f.db")
    assert asyncio.run(_adapter(store, [_pr()]).poll()) == 1
    item = store.list_items(log_tail=0)[0]
    assert item["branch"] == "51_add-bars" and item["intake_sha"] == "aaa"


def test_a_pr_from_a_fork_gets_no_branch(tmp_path):
    store = Store(tmp_path / "f.db")
    asyncio.run(_adapter(store, [_pr(cross=True)]).poll())
    assert store.list_items(log_tail=0)[0]["branch"] == ""


def test_a_fixed_pr_comes_in_again_once_its_crate_ships_with_its_loops(tmp_path):
    store = Store(tmp_path / "f.db")
    asyncio.run(_adapter(store, [_pr()]).poll())
    first = store.list_items(log_tail=0)[0]
    store.update_item(first["id"], state="shipped", loops=1)
    assert asyncio.run(_adapter(store, [_pr()]).poll()) == 0  # unchanged: not again
    assert asyncio.run(_adapter(store, [_pr(sha="bbb")]).poll()) == 1  # the Coder pushed a fix
    again = store.list_items(log_tail=0)[-1]
    assert again["id"] != first["id"] and again["loops"] == 1 and again["branch"] == "51_add-bars"
    assert asyncio.run(_adapter(store, [_pr(sha="bbb")]).poll()) == 0  # its new crate is live


def test_the_live_crate_owns_a_shared_branch():
    items = {9: {"branch": "b", "state": "running"}, 12: {"branch": "b", "state": "shipped"}}
    assert branch_names.owners(items) == {"b": 9}


# --- make_branch.py: a branch only on origin is checked out from there -----------------------

def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_make_branch_checks_out_the_prs_branch_from_origin(tmp_path):
    origin, other, repo = tmp_path / "origin.git", tmp_path / "other", tmp_path / "repo"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    for args in (["config", "user.email", "t@t"], ["config", "user.name", "t"],
                 ["commit", "-q", "--allow-empty", "-m", "base"], ["push", "-q", "origin", "HEAD:main"],
                 ["checkout", "-q", "-b", "pr-branch"], ["commit", "-q", "--allow-empty", "-m", "pr work"],
                 ["push", "-q", "origin", "pr-branch"]):
        _git(other, *args)
    _git(tmp_path, "clone", "-q", str(origin), str(repo))
    wt = tmp_path / "wt"
    out = subprocess.run([sys.executable, str(SCRIPTS / "make_branch.py"), "pr-branch", str(wt), "origin/main"],
                         cwd=repo, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "from origin" in out.stdout
    assert _git(wt, "log", "-1", "--format=%s") == "pr work"
    # Run again (back for rework): picks the worktree up where it was.
    again = subprocess.run([sys.executable, str(SCRIPTS / "make_branch.py"), "pr-branch", str(wt), "origin/main"],
                           cwd=repo, capture_output=True, text=True)
    assert again.returncode == 0 and "where it was" in again.stdout


# --- engine: the red belt Merger -> Coder is capped ------------------------------------------

class _Store:
    def __init__(self, item):
        self.item = item

    def get_item(self, item_id):
        return self.item

    def update_item(self, item_id, **fields):
        self.item.update(fields)


def _engine(loops):
    e = Engine.__new__(Engine)
    e.store = _Store({"id": 1, "loops": loops, "pr_url": "https://github.com/o/r/pull/65"})
    e.cfg = SimpleNamespace(max_rework=3, worktrees=Path("/nonexistent"))
    e.logged, e.scrapped = [], []

    async def log(item_id, machine, line):
        e.logged.append(line)

    async def scrap(item_id, reason, output="", kind="work_failed", via="", into="scrap"):
        e.scrapped.append(reason)
    e._log, e._scrap = log, scrap
    return e


LINE = SimpleNamespace(machines=[SimpleNamespace(id="merger", name="Merger")])


def test_red_belt_rides_are_counted():
    e = _engine(0)
    assert asyncio.run(e._red_belt_allowed(1, LINE, 0, "work_failed", "Merger: Merge PR", ""))
    assert e.store.item["loops"] == 1 and not e.scrapped
    assert "rework 1 of 3" in e.logged[-1]
    assert e._has_work(1)  # it has a PR: the Coder skips first-pass-only steps (TDD's tests)


def test_past_max_rework_it_is_scrapped_with_a_clear_note():
    e = _engine(3)
    assert not asyncio.run(e._red_belt_allowed(1, LINE, 0, "work_failed", "Merger: Merge PR", "conflicts"))
    assert e.scrapped and "over max_rework" in e.scrapped[0]
    assert "sent round for rework 3 times" in e.store.item["notes"]
