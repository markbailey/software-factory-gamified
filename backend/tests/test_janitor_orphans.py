"""The janitor removes leftover crate folders git no longer knows (even of a repo it wasn't told
about), never touches live crates or anything else, and says when git fails."""
import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from factory import accounts, janitor
from factory.config import JiraIntakeConfig, RepoConfig


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout


def _orphan(repo, wts, n):
    """crate-<n>: a worktree of repo whose git record is gone (git worktree list doesn't show it)."""
    _git(repo, "worktree", "add", "-q", "-b", f"crate/{n}", str(wts / f"crate-{n}"))
    import shutil
    shutil.rmtree(repo / ".git" / "worktrees" / f"crate-{n}")
    return wts / f"crate-{n}"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(janitor, "CLEANUP_FILE", tmp_path / "cleanup.json")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    wts = tmp_path / "worktrees"
    cfg = SimpleNamespace(worktrees=wts, default_repo=None, workspace=tmp_path / "ws")
    rc = RepoConfig(github="o/r", path=repo, base_branch="main", remote="")
    now = time.time()
    items = {37: {"id": 37, "state": "shipped", "repo": "o/r", "created_at": now},
             38: {"id": 38, "state": "running", "repo": "o/r", "created_at": now}}
    return cfg, rc, items, repo, wts


@pytest.mark.parametrize("listed", [False, True])
def test_orphan_of_shipped_crate_removed_even_if_repo_not_listed(world, tmp_path, listed):
    cfg, rc, items, repo, wts = world
    gone = _orphan(repo, wts, 37)
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    repos = [rc] if listed else [RepoConfig(github="x/y", path=other, base_branch="main", remote="")]
    removed = janitor.sweep(cfg, items, 24, repos)
    assert not gone.exists() and "worktree crate-37" in removed
    assert "crate/37" not in _git(repo, "branch", "--format=%(refname:short)").split()


def test_sweep_runs_with_no_repos_found(world):
    cfg, rc, items, repo, wts = world
    gone = _orphan(repo, wts, 37)
    missing = RepoConfig(github="z/z", path=wts.parent / "nope", base_branch="main", remote="")
    janitor.sweep(cfg, items, 24, [missing])
    assert not gone.exists()


def test_live_crate_and_other_folders_never_touched(world, tmp_path):
    cfg, rc, items, repo, wts = world
    live = _orphan(repo, wts, 38)
    _orphan(repo, wts, 37)
    odd = wts / "notacrate"
    odd.mkdir()
    nested = wts / "keep" / "crate-37"
    nested.mkdir(parents=True)
    outside = tmp_path / "crate-37"
    outside.mkdir()
    janitor.sweep(cfg, items, 24, [rc])
    assert live.exists() and odd.exists() and nested.exists() and outside.exists()
    assert not (wts / "crate-37").exists()


def test_inventory_fills_repo_of_leftover(world):
    cfg, rc, items, repo, wts = world
    _orphan(repo, wts, 37)
    (wts / "crate-99").mkdir()  # no .git file: the crate's repo field (no crate 99: stays blank)
    (wts / "crate-40").mkdir()
    items[40] = {"id": 40, "state": "shipped", "repo": "o/r", "created_at": time.time()}
    inv = janitor.inventory(cfg, items, [rc, rc])  # listed twice: scanned once
    rows = {r["name"]: r for r in inv["worktrees"]}
    assert rows["crate-37"]["repo"] == "o/r" and rows["crate-37"]["repo_path"] == str(repo)
    assert rows["crate-37"]["stale"]
    assert rows["crate-40"]["repo"] == "o/r" and rows["crate-40"]["repo_path"] == str(repo)
    assert rows["crate-99"]["repo"] == ""
    assert len([b for b in inv["branches"] if b["name"] == "crate/37"]) == 1


def test_clean_removes_leftover_with_known_repo(world):
    cfg, rc, items, repo, wts = world
    gone = _orphan(repo, wts, 37)
    inv = janitor.inventory(cfg, items, [rc])
    key = next(r["key"] for r in inv["worktrees"] if r["name"] == "crate-37")
    out = janitor.clean(cfg, items, [rc], [key], force=False)
    assert not gone.exists() and not out["failed"]


def test_git_failure_is_reported(world, capsys):
    cfg, rc, items, repo, wts = world
    proc = janitor._git(repo, "no-such-command")
    assert proc.returncode != 0
    assert "[janitor] git no-such-command" in capsys.readouterr().out
    janitor._git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/nope", warn=False)
    assert capsys.readouterr().out == ""


def test_git_still_works_after_accounts_use(world, tmp_path):
    """Regression: the default GitHub account puts an empty GIT_CONFIG_VALUE_<n> (clear the
    helper list) in the environment; Windows dropped it from the real process environment, so
    every git the janitor ran (inheriting it) died with "missing config value"."""
    cfg, rc, items, repo, wts = world
    jira = JiraIntakeConfig(base_url="", email="")
    a, _ = accounts.upsert(accounts.empty(), "github", {"name": "Bot", "token": "ghp_test"})
    accounts.use(a, jira)
    try:
        assert os.environ.get("GIT_CONFIG_COUNT")
        proc = janitor._git(repo, "branch", "--format=%(refname:short)")
        assert proc.returncode == 0, proc.stderr
        assert "main" in proc.stdout.split()
    finally:
        accounts.use(accounts.empty(), jira)
