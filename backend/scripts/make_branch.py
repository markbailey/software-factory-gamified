"""Coder: make the crate's branch and worktree, or pick up the ones it already has.

Usage: make_branch.py <branch> <worktree> <base_ref>   (run in the shared repo checkout)

A crate picked up again after the backend stopped (or moved by a new layout) may already have
its branch and worktree: they're reused, so its work so far isn't lost and "a branch named
crate/N already exists" can't stop it. Run again from the scrap bin removes them first, so a
retried crate still starts fresh. A branch that's only on origin (a pull request's branch) is
checked out from there.
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
        # Behind its PR on origin (someone pushed to it since): catch up first, so the push
        # after this crate's work can't throw those commits away.
        remote = f"origin/{branch}"
        if (run("rev-parse", "--verify", "--quiet", f"refs/remotes/{remote}").returncode == 0
                and run("merge-base", "--is-ancestor", "HEAD", remote, cwd=worktree).returncode == 0
                and run("merge-base", "--is-ancestor", remote, "HEAD", cwd=worktree).returncode != 0):
            ff = run("merge", "--ff-only", remote, cwd=worktree)
            print((ff.stdout + ff.stderr).strip(), flush=True)
            print(f"Brought {branch} up to date with {remote}.", flush=True)
    sys.exit(out.returncode)
# A crate that came in with an open PR (or whose local branch was cleaned up) has its branch on
# origin already: carry on from it, so a fix (like merging the base in for conflicts) lands on the
# PR, instead of a new branch from the base that would open a second PR.
if run("rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}").returncode == 0:
    out = run("worktree", "add", "-b", branch, str(worktree), f"origin/{branch}")
    print((out.stdout + out.stderr).strip(), flush=True)
    if out.returncode == 0:
        print(f"Picked up the branch {branch} from origin (the pull request's branch).", flush=True)
    sys.exit(out.returncode)
out = run("worktree", "add", "-b", branch, str(worktree), base_ref)
print((out.stdout + out.stderr).strip(), flush=True)
sys.exit(out.returncode)
