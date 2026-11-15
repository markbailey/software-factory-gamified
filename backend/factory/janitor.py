"""Janitor: removes worktrees and local branches that no crate needs any more.

Also removes the review folders (workspace/reviews/crate-<n>) of review-line crates by the
same rules. Runs at startup and then on a timer. A crate's worktree is removed when the crate has
shipped, when it has sat in the scrap bin longer than keep_scrapped_hours (a retry makes
a fresh one anyway), or when no crate in the database owns it. Crates still in the line
are never touched. Every pipeline's repo is swept: worktrees all live in one folder, and each
repo's crate/<n> branches are checked.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from .config import Config, RepoConfig
from .store import Store

CRATE_DIR_RE = re.compile(r"^crate-(\d+)$")
CRATE_BRANCH_RE = re.compile(r"^crate/(\d+)$")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    git = shutil.which("git") or "git"
    return subprocess.run([git, *args], cwd=repo, capture_output=True, text=True)


def _owner(worktree: Path, repos: list[Path]) -> Path | None:
    """The repo a worktree belongs to, from its .git file ("gitdir: <repo>/.git/worktrees/<name>")."""
    try:
        text = (worktree / ".git").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    gitdir = text.removeprefix("gitdir:").strip().replace("\\", "/")
    for repo in repos:
        if gitdir.lower().startswith((str(repo).replace("\\", "/") + "/.git/").lower()):
            return repo
    return None


def force_remove(path: Path) -> None:
    """Delete a folder even when Windows marks files read-only (git objects, node_modules)."""
    def make_writable(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if not path.exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable)
    else:
        shutil.rmtree(path, onerror=make_writable)


def _removable(item: dict | None, keep_scrapped_hours: float) -> bool:
    if item is None:
        return True  # nobody owns it
    if item["state"] == "shipped":
        return True
    if item["state"] == "scrapped":
        return time.time() - float(item.get("created_at", 0)) > keep_scrapped_hours * 3600
    return False  # still in the line, or waiting for you


def sweep(cfg: Config, items: dict[int, dict], keep_scrapped_hours: float,
          repos: list[RepoConfig] | None = None) -> list[str]:
    """One cleanup pass over the crates in items (id -> item), in every repo given (the default
    one if none). Returns what was removed."""
    removed: list[str] = []
    paths = [r.path for r in (repos or [cfg.default_repo]) if (r.path / ".git").exists()]
    if not paths:
        return removed

    if cfg.worktrees.exists():
        for path in sorted(cfg.worktrees.iterdir()):
            m = CRATE_DIR_RE.match(path.name)
            if not m or not path.is_dir():
                continue
            if not _removable(items.get(int(m.group(1))), keep_scrapped_hours):
                continue
            owner = _owner(path, paths)
            if owner is not None:
                _git(owner, "worktree", "remove", "--force", str(path))
            try:
                force_remove(path)
            except OSError as exc:
                print(f"[janitor] could not delete {path}: {exc}")
                continue
            removed.append(f"worktree {path.name}")

    for repo in paths:
        _git(repo, "worktree", "prune")

    # Review-line crates keep the PR and diff they read in workspace/reviews/crate-<n>.
    reviews = cfg.workspace / "reviews"
    if reviews.exists():
        for path in sorted(reviews.iterdir()):
            m = CRATE_DIR_RE.match(path.name)
            if not m or not path.is_dir() or not _removable(items.get(int(m.group(1))), keep_scrapped_hours):
                continue
            try:
                force_remove(path)
            except OSError as exc:
                print(f"[janitor] could not delete {path}: {exc}")
                continue
            removed.append(f"review folder {path.name}")

    for repo in paths:
        for branch in _git(repo, "branch", "--format=%(refname:short)").stdout.split():
            m = CRATE_BRANCH_RE.match(branch)
            if not m or not _removable(items.get(int(m.group(1))), keep_scrapped_hours):
                continue
            if _git(repo, "branch", "-D", branch).returncode == 0:
                removed.append(f"branch {branch}" + ("" if repo == paths[0] else f" in {repo.name}"))
    return removed


async def run(cfg: Config, store: Store, interval_minutes: float, keep_scrapped_hours: float,
              repos=None) -> None:
    """repos: a function giving the repos to sweep (the factory's lines' repos)."""
    while True:
        try:
            # Read the store here: its SQLite connection belongs to this thread.
            items = {item["id"]: item for item in store.list_items()}
            removed = await asyncio.to_thread(sweep, cfg, items, keep_scrapped_hours, repos() if repos else None)
            if removed:
                print(f"[janitor] removed {', '.join(removed)}")
        except Exception as exc:
            print(f"[janitor] cleanup failed: {exc}")
        await asyncio.sleep(max(60.0, interval_minutes * 60))
