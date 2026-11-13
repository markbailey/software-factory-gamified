"""Janitor: removes worktrees and local branches that no crate needs any more.

Runs at startup and then on a timer. A crate's worktree is removed when the crate has
shipped, when it has sat in the scrap bin longer than keep_scrapped_hours (a retry makes
a fresh one anyway), or when no crate in the database owns it. Crates still in the line
are never touched.
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

from .config import Config
from .store import Store

CRATE_DIR_RE = re.compile(r"^crate-(\d+)$")
CRATE_BRANCH_RE = re.compile(r"^crate/(\d+)$")


def _git(cfg: Config, *args: str) -> subprocess.CompletedProcess:
    git = shutil.which("git") or "git"
    return subprocess.run([git, *args], cwd=cfg.repo, capture_output=True, text=True)


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


def sweep(cfg: Config, items: dict[int, dict], keep_scrapped_hours: float) -> list[str]:
    """One cleanup pass over the crates in items (id -> item). Returns what was removed."""
    removed: list[str] = []
    if not (cfg.repo / ".git").exists():
        return removed

    if cfg.worktrees.exists():
        for path in sorted(cfg.worktrees.iterdir()):
            m = CRATE_DIR_RE.match(path.name)
            if not m or not path.is_dir():
                continue
            if not _removable(items.get(int(m.group(1))), keep_scrapped_hours):
                continue
            _git(cfg, "worktree", "remove", "--force", str(path))
            try:
                force_remove(path)
            except OSError as exc:
                print(f"[janitor] could not delete {path}: {exc}")
                continue
            removed.append(f"worktree {path.name}")

    _git(cfg, "worktree", "prune")

    branches = _git(cfg, "branch", "--format=%(refname:short)").stdout.split()
    for branch in branches:
        m = CRATE_BRANCH_RE.match(branch)
        if not m or not _removable(items.get(int(m.group(1))), keep_scrapped_hours):
            continue
        if _git(cfg, "branch", "-D", branch).returncode == 0:
            removed.append(f"branch {branch}")
    return removed


async def run(cfg: Config, store: Store, interval_minutes: float, keep_scrapped_hours: float) -> None:
    while True:
        try:
            # Read the store here: its SQLite connection belongs to this thread.
            items = {item["id"]: item for item in store.list_items()}
            removed = await asyncio.to_thread(sweep, cfg, items, keep_scrapped_hours)
            if removed:
                print(f"[janitor] removed {', '.join(removed)}")
        except Exception as exc:
            print(f"[janitor] cleanup failed: {exc}")
        await asyncio.sleep(max(60.0, interval_minutes * 60))
