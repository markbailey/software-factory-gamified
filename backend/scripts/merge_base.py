"""Coder: bring the latest base branch into the crate's branch before Claude works on it.

Usage: merge_base.py <base_ref>   (run in the crate's worktree)

Nothing to do on a fresh branch. A crate sent back because its pull request has merge
conflicts gets the base merged in here; where the two clash, the files are left with conflict
markers for Claude to resolve (the Coder's commit then finishes the merge). Always exits 0
unless git itself is broken.
"""
import shutil
import subprocess
import sys

base_ref = sys.argv[1]
git = shutil.which("git")
if not git:
    print("git is not installed or not on PATH", flush=True)
    sys.exit(1)


def run(*args):
    return subprocess.run([git, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


if run("rev-parse", "--verify", "--quiet", "MERGE_HEAD").returncode == 0:
    print("A merge of the base branch is already under way; Claude finishes it.", flush=True)
    sys.exit(0)
if run("merge-base", "--is-ancestor", base_ref, "HEAD").returncode == 0:
    print(f"The branch already has everything on {base_ref}.", flush=True)
    sys.exit(0)
# Not committed here: the Coder's commit finishes the merge, with Claude's fixes in it (and
# "Check for changes" sees the merge even when Claude has nothing to add).
out = run("-c", "user.name=Software Factory", "-c", "user.email=factory@localhost",
          "merge", "--no-commit", "--no-ff", base_ref)
if out.returncode == 0:
    print(f"Merged {base_ref} into the branch, no conflicts.", flush=True)
    sys.exit(0)
clashes = run("diff", "--name-only", "--diff-filter=U").stdout.split()
if clashes:
    print(f"Merging {base_ref} clashes in: {', '.join(clashes)}. Claude resolves the conflict "
          "markers in those files.", flush=True)
    sys.exit(0)
print((out.stdout + out.stderr).strip(), flush=True)
sys.exit(1)
