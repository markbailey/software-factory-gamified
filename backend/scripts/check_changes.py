"""Coder: fail with Claude's own explanation when it changed nothing.

Runs after "git add -A" in the crate's worktree, with Claude's last reply on stdin. If nothing
is staged, prints "OUTCOME <needs_info|already_done|blocked>" (from the reply's
"Outcome: ..." line; needs_info when it has none), "Claude made no changes" and Claude's reply,
then exits 1. The factory uses the outcome to decide what happens to the crate and its issue.
"""
import re
import shutil
import subprocess
import sys

reply = sys.stdin.buffer.read().decode("utf-8", errors="replace").strip()
git = shutil.which("git")
if not git:
    print("git is not installed or not on PATH", flush=True)
    sys.exit(1)
staged = subprocess.run([git, "diff", "--cached", "--quiet"])
if staged.returncode == 0:  # nothing staged
    m = re.search(r"^\W*outcome\W*\s*(needs[ _-]?info|already[ _-]?done|blocked)\b", reply,
                  re.IGNORECASE | re.MULTILINE)
    outcome = re.sub(r"[ -]", "_", m.group(1).lower()).replace("needsinfo", "needs_info") \
        .replace("alreadydone", "already_done") if m else "needs_info"
    print(f"OUTCOME {outcome}", flush=True)
    print("Claude made no changes.", flush=True)
    print("Its explanation:" if reply else "It gave no explanation.", flush=True)
    if reply:
        print(reply, flush=True)
    sys.exit(1)
markers = subprocess.run([git, "diff", "--cached", "-G", r"^(<<<<<<<|>>>>>>>) ", "--name-only"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.split()
if markers:
    print(f"Merge conflict markers are still in: {', '.join(markers)}. The Coder must resolve them.", flush=True)
    sys.exit(1)
files = subprocess.run([git, "diff", "--cached", "--name-only"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace").stdout.split()
print(f"{len(files)} file(s) changed.", flush=True)
