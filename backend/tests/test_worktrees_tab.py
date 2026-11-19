"""The dashboard's Worktrees & branches tab: what counts as stale, and Clean / Force clean."""
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from factory import janitor
from factory.config import RepoConfig


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(janitor, "CLEANUP_FILE", tmp_path / "cleanup.json")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    wts = tmp_path / "worktrees"
    for n in (1, 2, 3):  # 1 shipped, 2 still in the line, 3 owned by nobody
        _git(repo, "worktree", "add", "-q", "-b", f"crate/{n}", str(wts / f"crate-{n}"))
    _git(repo, "branch", "merged-feature")  # at main: merged
    _git(repo, "checkout", "-q", "-b", "wip")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "wip")
    _git(repo, "checkout", "-q", "main")
    mine = tmp_path / "mine"
    _git(repo, "worktree", "add", "-q", str(mine), "wip")
    (mine / "notes.txt").write_text("uncommitted")
    (wts / "crate-9").mkdir()  # a half-deleted leftover
    cfg = SimpleNamespace(worktrees=wts, default_repo=None)
    repos = [RepoConfig(github="o/r", path=repo, base_branch="main", remote="")]
    items = {1: {"id": 1, "state": "shipped", "pr_url": "u", "pr_state": "MERGED", "created_at": time.time()},
             2: {"id": 2, "state": "running", "created_at": time.time()}}
    return cfg, repos, items, repo, mine


def _rows(inv):
    return {r["name"]: r for r in inv["worktrees"]} | {"b:" + r["name"]: r for r in inv["branches"]}


def test_inventory_says_what_is_stale(world):
    cfg, repos, items, repo, mine = world
    rows = _rows(janitor.inventory(cfg, items, repos))
    assert rows["crate-1"]["stale"] and "PR merged" in rows["crate-1"]["reasons"]
    assert rows["crate-2"]["in_use"] and not rows["crate-2"]["stale"] and not rows["crate-2"]["can_force"]
    assert rows["crate-3"]["stale"] and rows["crate-3"]["reasons"] == ["no crate owns it"]
    assert rows["crate-9"]["stale"]  # a folder git doesn't know
    assert not rows["mine"]["stale"] and rows["mine"]["can_force"] and rows["mine"]["dirty"] == 1
    assert rows["b:main"]["protected"] and not rows["b:main"]["can_force"]
    assert rows["b:merged-feature"]["stale"] and rows["b:merged-feature"]["reasons"] == ["merged into main"]
    assert not rows["b:wip"]["stale"]
    assert rows["b:crate/2"]["in_use"]


def test_idle_branch_is_stale(world, monkeypatch):
    cfg, repos, items, repo, mine = world
    monkeypatch.setattr(janitor.time, "time", lambda: 4e9)  # far in the future
    assert _rows(janitor.inventory(cfg, items, repos))["b:wip"]["stale"]


def test_clean_takes_only_stale_and_force_takes_the_rest(world):
    cfg, repos, items, repo, mine = world
    inv = janitor.inventory(cfg, items, repos)
    keys = [r["key"] for r in inv["worktrees"] + inv["branches"]]
    out = janitor.clean(cfg, items, repos, keys, force=False)
    assert not (cfg.worktrees / "crate-1").exists() and not (cfg.worktrees / "crate-3").exists()
    assert not (cfg.worktrees / "crate-9").exists()
    assert (cfg.worktrees / "crate-2").exists() and mine.exists()
    branches = _git(repo, "branch", "--format=%(refname:short)").split()
    assert "crate/1" not in branches and "crate/3" not in branches and "merged-feature" not in branches
    assert {"crate/2", "main", "wip"} <= set(branches)
    failed = {f["what"] for f in out["failed"]}
    assert {"crate-2", "mine", "main", "wip", "crate/2"} <= failed

    inv = janitor.inventory(cfg, items, repos)
    keys = [r["key"] for r in inv["worktrees"] + inv["branches"]]
    out = janitor.clean(cfg, items, repos, keys, force=True)
    assert not mine.exists()
    branches = _git(repo, "branch", "--format=%(refname:short)").split()
    assert "wip" not in branches  # unmerged, gone with force
    assert {"crate/2", "main"} <= set(branches) and (cfg.worktrees / "crate-2").exists()


def test_gentle_clean_keeps_uncommitted_changes(world, monkeypatch):
    cfg, repos, items, repo, mine = world
    monkeypatch.setattr(janitor.time, "time", lambda: 4e9)  # wip (and its worktree) idle, so stale
    row = _rows(janitor.inventory(cfg, items, repos))["mine"]
    assert row["stale"]
    out = janitor.clean(cfg, items, repos, [row["key"]], force=False)
    assert mine.exists() and "Force clean" in out["failed"][0]["error"]
