"""Reviewer, step 1 when there's no pull request yet: save a branch's changes for Claude to read.

Usage: collect_diff.py <worktree> <out-dir> <base-ref> <branch>
Writes <out-dir>/pr.md (branch, commits, changed files) and <out-dir>/diff.patch (everything
on the branch since it left base-ref, plus anything not committed yet). Exit 1 if there's
nothing to review.
"""
import shutil
import subprocess
import sys
from pathlib import Path

worktree, out, base, branch = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
git = shutil.which("git")
if not git:
    print("git is not installed or not on PATH", flush=True)
    sys.exit(1)
if not worktree.is_dir():
    print(f"There's no branch to review: {worktree} doesn't exist (is there a Coder before the Reviewer?)",
          flush=True)
    sys.exit(1)


def run(*args: str) -> str:
    r = subprocess.run([git, *args], cwd=worktree, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        print(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()}", flush=True)
        sys.exit(1)
    return r.stdout


start = run("merge-base", base, "HEAD").strip()
diff = run("diff", start)  # committed on the branch and not yet committed, against where it began
if not diff.strip():
    print(f"Nothing to review: {branch} has no changes since {base}.", flush=True)
    sys.exit(1)
log = run("log", "--format=- %s", f"{start}..HEAD").strip()
files = run("diff", "--stat", start).strip()
out.mkdir(parents=True, exist_ok=True)
(out / "diff.patch").write_text(diff, encoding="utf-8")
(out / "pr.md").write_text(
    f"# Changes on {branch} (no pull request yet)\n\nBase: {base}\n\n## Commits\n\n{log or '(none yet)'}\n\n"
    f"## Changed files\n\n```\n{files}\n```\n", encoding="utf-8")
print(f"Saved the diff of {branch} against {base} ({len(diff.splitlines())} lines).", flush=True)
