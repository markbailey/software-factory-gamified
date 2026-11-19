"""Janitor: removes worktrees and local branches that no crate needs any more.

Also removes the review folders (workspace/reviews/crate-<n>) of review-line crates by the
same rules. Runs at startup and then on a timer. A crate's worktree is removed when the crate has
shipped, when it has sat in the scrap bin longer than keep_scrapped_hours (a retry makes
a fresh one anyway), or when no crate in the database owns it. Crates still in the line
are never touched. Every pipeline's repo is swept: worktrees all live in one folder, and each
repo's crate branches (crate/<n>, or the name the branch naming pattern gave) are checked.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import branch_names
from .config import BACKEND_DIR, Config, RepoConfig
from .store import Store

# Force delete (a tick box under the gear in the game; off by default). Off, removing a crate's
# worktree is what it always was: git worktree remove --force, then one try at deleting what's
# left. On, it also removes locked worktrees (git worktree remove -f -f), stops programs still
# running from inside the folder (a dev server or watcher a machine left behind, which is what
# keeps Windows from deleting node_modules), and keeps retrying files Windows holds open.
CLEANUP_FILE = BACKEND_DIR / "cleanup.json"
FORCE_RETRIES = 5

CRATE_DIR_RE = re.compile(r"^crate-(\d+)$")
CRATE_BRANCH_RE = re.compile(r"^crate/(\d+)$")


def _git(repo: Path, *args: str, warn: bool = True) -> subprocess.CompletedProcess:
    """git in repo. A failure that says why is logged in one line (warn=False: the caller expects
    some, e.g. rev-parse --verify, or reports them itself)."""
    git = shutil.which("git") or "git"
    try:
        proc = subprocess.run([git, *args], cwd=repo, capture_output=True, text=True)
    except OSError as exc:  # no such folder, or no git
        proc = subprocess.CompletedProcess([git, *args], 1, "", str(exc))
    if warn and proc.returncode != 0 and (proc.stderr or "").strip():
        print(f"[janitor] git {' '.join(args[:2])} in {repo} failed ({proc.returncode}): {_err(proc)}")
    return proc


def _gitdir(worktree: Path) -> str:
    try:
        text = (worktree / ".git").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    return text.removeprefix("gitdir:").strip().replace("\\", "/") if text.startswith("gitdir:") else ""


def _owner(worktree: Path, repos: list[Path]) -> Path | None:
    """The repo a worktree belongs to, from its .git file ("gitdir: <repo>/.git/worktrees/<name>")."""
    gitdir = _gitdir(worktree)
    if not gitdir:
        return None
    for repo in repos:
        if gitdir.lower().startswith((str(repo).replace("\\", "/") + "/.git/").lower()):
            return repo
    return None


def _gitdir_repo(worktree: Path) -> Path | None:
    """The repo named by a worktree's .git file, whether or not it's one of the lines' repos
    (only if that repo is still there)."""
    gitdir = _gitdir(worktree)
    at = gitdir.lower().rfind("/.git/worktrees/")
    if at <= 0:
        return None
    repo = Path(gitdir[:at])
    return repo if (repo / ".git").exists() else None


def _unique(paths: list[Path]) -> list[Path]:
    """paths without repeats (a repo listed twice, under another spelling)."""
    seen: dict[str, Path] = {}
    for p in paths:
        seen.setdefault(_norm(p), p)
    return list(seen.values())


def force_delete_on() -> bool:
    """Whether force delete is ticked (read each time: the game can change it at any moment)."""
    try:
        return bool(json.loads(CLEANUP_FILE.read_text(encoding="utf-8")).get("force_delete", False))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def set_force_delete(on: bool) -> None:
    CLEANUP_FILE.write_text(json.dumps({"force_delete": bool(on)}), encoding="utf-8")


def remove_args(path: Path, force: bool) -> list[list[str]]:
    """The git commands (after "git") that remove the worktree at path."""
    if force:  # unlock first; a doubled --force removes a locked worktree too
        return [["worktree", "unlock", str(path)], ["worktree", "remove", "--force", "--force", str(path)]]
    return [["worktree", "remove", "--force", str(path)]]


def force_remove(path: Path, stubborn: bool = False, stop: bool | None = None) -> None:
    """Delete a folder even when Windows marks files read-only (git objects, node_modules).
    stubborn: use long paths on Windows (node_modules goes past 260 characters) and retry while
    files are still held open; and, unless stop is False, first stop programs running from inside
    it (stop defaults to stubborn: force delete)."""
    def make_writable(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if not path.exists():
        return
    stop = stubborn if stop is None else stop
    target = path
    if stubborn:
        if stop:
            stop_processes_in(path)
        if os.name == "nt" and not str(path).startswith("\\\\?\\"):
            target = Path("\\\\?\\" + str(path.resolve()))
    tries = FORCE_RETRIES if stubborn else 1
    for attempt in range(tries):
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(target, onexc=make_writable)
            else:
                shutil.rmtree(target, onerror=make_writable)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == tries - 1 or not path.exists():
                if path.exists():
                    raise
                return
            time.sleep(0.5 * (attempt + 1))  # Windows lets go of a file a moment after its program ends
            if attempt == 1 and stop:
                stop_processes_in(path)  # something may have started again


def _processes_in(path: Path) -> list[int]:
    """Programs whose executable or command line is inside path (never this backend)."""
    folder = str(path.resolve())
    mine = {os.getpid(), os.getppid()}
    pids: list[int] = []
    if os.name == "nt":
        variants = [folder.rstrip("\\") + "\\", folder.rstrip("\\").replace("\\", "/") + "/"]
        test = " -or ".join(
            f"($_.ExecutablePath -and $_.ExecutablePath.StartsWith('{v}', 'OrdinalIgnoreCase'))"
            f" -or ($_.CommandLine -and $_.CommandLine.IndexOf('{v}', [StringComparison]::OrdinalIgnoreCase) -ge 0)"
            for v in (x.replace("'", "''") for x in variants))
        command = f"Get-CimInstance Win32_Process | Where-Object {{ {test} }} | ForEach-Object {{ $_.ProcessId }}"
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                                 capture_output=True, text=True, timeout=30,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        pids = [int(x) for x in out.split() if x.isdigit()]
    else:
        prefix = folder.rstrip("/") + "/"
        for proc in Path("/proc").glob("[0-9]*"):
            try:
                places = [os.readlink(proc / "cwd"), os.readlink(proc / "exe")]
            except OSError:
                continue
            if any((p + "/").startswith(prefix) for p in places):
                pids.append(int(proc.name))
    return [pid for pid in pids if pid not in mine]


def stop_processes_in(path: Path) -> list[int]:
    """Force delete: end the programs (and their children) still running from inside path."""
    pids = _processes_in(path)
    for pid in pids:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                os.kill(pid, 9)
        except (OSError, subprocess.SubprocessError):
            pass
    if pids:
        print(f"[janitor] stopped {len(pids)} program(s) running in {path}")
    return pids


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
    force = force_delete_on()
    paths = _unique([r.path for r in (repos or [cfg.default_repo]) if r is not None and (r.path / ".git").exists()])
    others: list[Path] = []  # repos named by a crate folder's .git file but not in the list

    # Only direct children of the worktrees folder named crate-<n>, whose crate isn't live.
    if cfg.worktrees.exists():
        for path in sorted(cfg.worktrees.iterdir()):
            m = CRATE_DIR_RE.match(path.name)
            if not m or not path.is_dir():
                continue
            item = items.get(int(m.group(1)))
            if (item is not None and item.get("state") in LIVE) or not _removable(item, keep_scrapped_hours):
                continue
            owner = _owner(path, paths) or _gitdir_repo(path)
            if owner is not None:
                if _norm(owner) not in {_norm(p) for p in paths + others}:
                    others.append(owner)
                for args in remove_args(path, force):
                    _git(owner, *args, warn=False)  # git may not know it any more
            try:
                # Long paths and retries always; stopping programs only with Force delete.
                force_remove(path, stubborn=True, stop=force)
            except OSError as exc:
                print(f"[janitor] could not delete {path}: {exc}"
                      + ("" if force else " (tick Force delete under the gear in the game to stop what holds it)"))
                continue
            removed.append(f"worktree {path.name}")

    paths += others
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
                force_remove(path, stubborn=force)
            except OSError as exc:
                print(f"[janitor] could not delete {path}: {exc}")
                continue
            removed.append(f"review folder {path.name}")

    owners = branch_names.owners(items)  # crates' branches, named by the branch naming pattern
    for repo in paths:
        for branch in _git(repo, "branch", "--format=%(refname:short)").stdout.split():
            crate = branch_names.crate_of(branch, owners)
            if crate is None or not _removable(items.get(crate), keep_scrapped_hours):
                continue
            if _git(repo, "branch", "-D", branch, warn=False).returncode == 0:
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


# The dashboard's Worktrees & branches tab -------------------------------------------------
#
# Lists every worktree and local branch in the lines' repos (and leftover folders under the
# worktrees folder), says whether each is stale, and removes the ones you tick.
#
# Stale means nothing needs it any more:
#   a crate's (crate-<n> / crate/<n>): its crate shipped or was scrapped, or no crate owns it,
#     or its PR was merged or closed. A crate still in the line (or waiting for you) is in use
#     and is never removed from here, not even by Force clean: scrap the crate first.
#   anything else: merged into the base branch, its remote branch is gone (the PR was merged
#     and the branch deleted), or no commits for IDLE_DAYS days. A worktree folder git no
#     longer knows about, or a worktree whose folder is gone, is stale too.
# The base branch and whatever each repo's own checkout has checked out are never removed.
#
# Clean removes stale ones gently: crate leftovers the way the janitor does, but someone else's
# worktree only if it has no uncommitted changes, and someone else's branch only if git agrees
# it's merged. Force clean also takes rows that aren't stale (except in use and protected ones)
# and uses the Force delete path: unlock, git worktree remove -f -f, stop programs running in
# the folder, retry files Windows holds open, git branch -D.

IDLE_DAYS = 14
LIVE = ("queued", "waiting", "moving", "running", "awaiting_approval", "held")


def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def _base_ref(repo: RepoConfig) -> str:
    """The ref "merged" is measured against: origin/<base> when it's there (it's newer), else <base>."""
    base = repo.base_branch
    if not base:
        head = _git(repo.path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", warn=False).stdout.strip()
        base = head.removeprefix("origin/") if head else ""
    if not base:
        for guess in ("main", "master"):
            if _git(repo.path, "rev-parse", "--verify", "--quiet", f"refs/heads/{guess}", warn=False).returncode == 0:
                base = guess
                break
    if not base:
        return ""
    if _git(repo.path, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{base}", warn=False).returncode == 0:
        return f"origin/{base}"
    return base


def _worktree_entries(repo: Path) -> list[dict]:
    """git worktree list --porcelain, as dicts; the first is the repo's own checkout."""
    out: list[dict] = []
    cur: dict | None = None
    for line in _git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[len("worktree "):].strip(), "branch": "", "locked": False, "prunable": False,
                   "detached": False, "bare": False}
            out.append(cur)
        elif cur is None:
            continue
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):].strip().removeprefix("refs/heads/")
        elif line.startswith("locked"):
            cur["locked"] = True
        elif line.startswith("prunable"):
            cur["prunable"] = True
        elif line == "detached":
            cur["detached"] = True
        elif line == "bare":
            cur["bare"] = True
    return out


def _crate_verdict(crate_id: int, items: dict[int, dict]) -> tuple[bool, list[str], dict]:
    """(in use, stale reasons, crate facts) for something named after crate <crate_id>."""
    item = items.get(crate_id)
    if item is None:
        return False, ["no crate owns it"], {"crate": crate_id, "crate_state": ""}
    facts = {"crate": crate_id, "crate_state": item["state"], "pr_url": str(item.get("pr_url") or ""),
             "pr_state": str(item.get("pr_state") or "").lower()}
    if item["state"] in LIVE:
        return True, [], facts
    reasons = [f"crate #{crate_id} {item['state']}"]
    if facts["pr_state"] in ("merged", "closed"):
        reasons.append(f"PR {facts['pr_state']}")
    return False, reasons, facts


def _idle_reason(last: float, now: float) -> list[str]:
    days = (now - last) / 86400 if last else 0
    return [f"no commits for {int(days)} days"] if last and days >= IDLE_DAYS else []


def inventory(cfg: Config, items: dict[int, dict], repos: list[RepoConfig] | None = None) -> dict:
    """Every worktree and local branch, with whether it's stale and why (see above)."""
    now = time.time()
    unique: dict[str, RepoConfig] = {}  # a repo listed twice is scanned once
    for r in (repos or [cfg.default_repo]):
        if r is not None and (r.path / ".git").exists():
            unique.setdefault(_norm(r.path), r)
    repos = list(unique.values())
    worktrees: list[dict] = []
    branches: list[dict] = []
    known: set[str] = set()
    owners = branch_names.owners(items)  # crates' branches, named by the branch naming pattern
    for repo in repos:
        base_ref = _base_ref(repo)
        base = base_ref.removeprefix("origin/")
        merged = set()
        if base_ref:
            merged = set(_git(repo.path, "branch", "--merged", base_ref, "--format=%(refname:short)").stdout.split())
        entries = _worktree_entries(repo.path)
        main_branch = entries[0]["branch"] if entries else ""
        checked_out = {e["branch"]: e["path"] for e in entries[1:] if e["branch"]}
        label = repo.github or repo.path.name

        branch_info: dict[str, dict] = {}
        refs = _git(repo.path, "for-each-ref", "--format=%(refname:short)\t%(committerdate:unix)\t%(upstream:track)",
                    "refs/heads").stdout
        for line in refs.splitlines():
            parts = line.split("\t")
            if not parts or not parts[0]:
                continue
            name = parts[0]
            last = float(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0.0
            gone = len(parts) > 2 and "gone" in parts[2]
            row = {"kind": "branch", "key": f"B|{_norm(repo.path)}|{name}", "repo": label, "repo_path": str(repo.path),
                   "name": name, "last": last, "merged": name in merged and name != base, "gone": gone,
                   "worktree": checked_out.get(name, ""), "in_use": False, "protected": "", "reasons": [],
                   "crate": None, "crate_state": "", "pr_url": "", "pr_state": ""}
            crate = branch_names.crate_of(name, owners)
            if name == base:
                row["protected"] = "the base branch"
            elif name == main_branch:
                row["protected"] = f"checked out in {label}'s own checkout"
            elif crate is not None:
                in_use, reasons, facts = _crate_verdict(crate, items)
                row.update(facts, in_use=in_use, reasons=reasons)
            else:
                if row["merged"]:
                    row["reasons"].append(f"merged into {base}")
                if gone:
                    row["reasons"].append("its remote branch is gone")
                row["reasons"] += _idle_reason(last, now)
            branch_info[name] = row
            branches.append(row)

        for e in entries[1:]:
            if e["bare"]:
                continue
            path = Path(e["path"])
            known.add(_norm(path))
            last = 0.0
            dirty = 0
            if path.is_dir():
                head = _git(path, "log", "-1", "--format=%ct").stdout.strip()
                last = float(head) if head.isdigit() else 0.0
                try:
                    last = max(last, path.stat().st_mtime)
                except OSError:
                    pass
                dirty = len(_git(path, "status", "--porcelain").stdout.splitlines())
            row = {"kind": "worktree", "key": f"W|{_norm(repo.path)}|{_norm(path)}", "repo": label,
                   "repo_path": str(repo.path), "path": str(path), "name": path.name, "branch": e["branch"],
                   "last": last, "dirty": dirty, "locked": e["locked"], "missing": e["prunable"] or not path.is_dir(),
                   "in_use": False, "protected": "", "reasons": [], "crate": None, "crate_state": "",
                   "pr_url": "", "pr_state": ""}
            m = CRATE_DIR_RE.match(path.name) if _norm(path.parent) == _norm(cfg.worktrees) else None
            if row["missing"]:
                row["reasons"].append("its folder is gone")
            if m:
                in_use, reasons, facts = _crate_verdict(int(m.group(1)), items)
                row.update(facts, in_use=in_use)
                row["reasons"] += reasons if not in_use else []
            elif not row["missing"]:
                b = branch_info.get(e["branch"])
                if b is not None:
                    row["reasons"] += b["reasons"]
                else:  # detached
                    row["reasons"] += _idle_reason(last, now)
            worktrees.append(row)

    # Folders under the worktrees folder that no repo knows as a worktree (a half-deleted one).
    if cfg.worktrees.exists():
        for path in sorted(cfg.worktrees.iterdir()):
            if not path.is_dir() or _norm(path) in known:
                continue
            row = {"kind": "worktree", "key": f"W||{_norm(path)}", "repo": "", "repo_path": "", "path": str(path),
                   "name": path.name, "branch": "", "last": path.stat().st_mtime, "dirty": 0, "locked": False,
                   "missing": False, "in_use": False, "protected": "", "reasons": ["git no longer knows it (a leftover folder)"],
                   "crate": None, "crate_state": "", "pr_url": "", "pr_state": ""}
            m = CRATE_DIR_RE.match(path.name)
            if m:
                in_use, _reasons, facts = _crate_verdict(int(m.group(1)), items)
                row.update(facts, in_use=in_use)
                if in_use:
                    row["reasons"] = []
            # Whose it was: the repo its .git file names, else the crate's repo.
            owner = _gitdir_repo(path)
            if owner is not None:
                known_repo = unique.get(_norm(owner))
                row["repo"] = (known_repo.github if known_repo else "") or owner.name
                row["repo_path"] = str(owner)
            elif m and (items.get(int(m.group(1))) or {}).get("repo"):
                label = str(items[int(m.group(1))]["repo"])
                row["repo"] = label
                match = [r for r in repos if (r.github or "").lower() == label.lower()]
                row["repo_path"] = str(match[0].path) if match else ""
            worktrees.append(row)

    for row in worktrees + branches:
        row["stale"] = bool(row["reasons"]) and not row["in_use"] and not row["protected"]
        row["can_clean"] = row["stale"]
        row["can_force"] = not row["in_use"] and not row["protected"]
    return {"type": "worktrees", "worktrees": worktrees, "branches": branches, "idle_days": IDLE_DAYS,
            "worktrees_folder": str(cfg.worktrees), "checked_at": now}


def _err(proc: subprocess.CompletedProcess) -> str:
    text = (proc.stderr or proc.stdout or "").strip().splitlines()
    return text[-1].removeprefix("error: ").removeprefix("fatal: ") if text else f"git exited {proc.returncode}"


def clean(cfg: Config, items: dict[int, dict], repos: list[RepoConfig] | None, keys: list[str],
          force: bool) -> dict:
    """Remove the worktrees and branches with these keys (from inventory). Each is checked again
    first: Clean only takes stale ones, Force clean anything that isn't in use or protected.
    Worktrees go first, so their branches can go after."""
    inv = inventory(cfg, items, repos)
    rows = {r["key"]: r for r in inv["worktrees"] + inv["branches"]}
    removed: list[str] = []
    failed: list[dict] = []
    wanted = [rows.get(k) or {"key": k} for k in dict.fromkeys(keys)]
    touched: set[str] = set()

    def fail(row: dict, why: str) -> None:
        failed.append({"key": row["key"], "what": row.get("name", row["key"]), "error": why})

    for row in [r for r in wanted if r.get("kind") == "worktree"] + [r for r in wanted if r.get("kind") == "branch"] \
            + [r for r in wanted if "kind" not in r]:
        if "kind" not in row:
            fail(row, "it isn't there any more")
            continue
        if row["in_use"]:
            fail(row, f"crate #{row['crate']} is still using it (scrap the crate first)")
            continue
        if row["protected"]:
            fail(row, f"it's {row['protected']}")
            continue
        if not force and not row["stale"]:
            fail(row, "it isn't stale (use Force clean to remove it anyway)")
            continue
        repo = Path(row["repo_path"]) if row["repo_path"] else None
        if row["kind"] == "worktree":
            path = Path(row["path"])
            ours = row["crate"] is not None or row["key"].startswith("W||")  # a crate's, or a leftover folder
            if repo is not None and not row["missing"]:
                if force or ours:
                    for args in remove_args(path, force):
                        _git(repo, *args, warn=False)  # a leftover git no longer knows
                else:  # someone's own worktree: git refuses if it has uncommitted changes
                    proc = _git(repo, "worktree", "remove", str(path))
                    if proc.returncode != 0:
                        fail(row, _err(proc) + " (Force clean throws those changes away)")
                        continue
            try:
                force_remove(path, stubborn=force)
            except OSError as exc:
                fail(row, f"couldn't delete the folder: {exc}"
                     + ("" if force else " (Force clean stops what holds it)"))
                continue
            if repo is not None:
                touched.add(str(repo))
                _git(repo, "worktree", "prune")
            removed.append(f"worktree {row['name']}" + (f" in {row['repo']}" if row["repo"] else ""))
        else:
            ours = row["crate"] is not None
            proc = _git(repo, "branch", "-D" if force or ours else "-d", row["name"])
            if proc.returncode != 0:
                why = _err(proc)
                if "not fully merged" in why:
                    why = "git says it isn't fully merged (Force clean deletes it anyway)"
                fail(row, why)
                continue
            removed.append(f"branch {row['name']} in {row['repo']}")
    for repo in touched:
        _git(Path(repo), "worktree", "prune")
    return {"type": "worktrees_cleaned", "removed": removed, "failed": failed, "force": force}
