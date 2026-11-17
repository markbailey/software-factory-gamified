"""Coder: make the crate's branch and worktree, or pick up the ones it already has.

Usage: make_branch.py <branch> <worktree> <base_ref>   (run in the shared repo checkout)

A crate picked up again after the backend stopped (or moved by a new layout) may already have
its branch and worktree: they're reused, so its work so far isn't lost and "a branch named
crate/N already exists" can't stop it. Run again from the scrap bin removes them first, so a
retried crate still starts fresh.
"""
import shutil
import subprocess
import sys
from pathlib import Path

branch, worktree, base_ref = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
git = shutil.which("git")
if not git:
    print("git is not installed or not on PATH", flush=True)
    sys.exit(1)


def run(*args, cwd=None):
    return subprocess.run([git, *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


if worktree.is_dir() and (worktree / ".git").exists():
    on = run("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree).stdout.strip()
    if on == branch:
        print(f"Picking up the crate's branch {branch} where it was ({worktree}).", flush=True)
        sys.exit(0)
    print(f"{worktree} is on {on or 'no branch'}, not {branch}; scrap the crate and run it again.", flush=True)
    sys.exit(1)
run("worktree", "prune")
if run("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
    out = run("worktree", "add", str(worktree), branch)
    print((out.stdout + out.stderr).strip(), flush=True)
    if out.returncode == 0:
        print(f"Picked up the existing branch {branch}.", flush=True)
    sys.exit(out.returncode)
out = run("worktree", "add", "-b", branch, str(worktree), base_ref)
print((out.stdout + out.stderr).strip(), flush=True)
sys.exit(out.returncode)
