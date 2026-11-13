"""Packager: open a pull request for the current branch, or reuse the one already open.

Usage: open_pr.py <base> <branch> <title>   (PR body on stdin)
Prints the PR URL. A retried crate pushes to the same branch, so its open PR updates itself.
"""
import json
import shutil
import subprocess
import sys

base, branch, title = sys.argv[1], sys.argv[2], sys.argv[3]
# Read the body as UTF-8 bytes: on Windows the default stdin encoding is cp1252,
# which mangles characters such as curly quotes.
body = sys.stdin.buffer.read().decode("utf-8", errors="replace")
gh = shutil.which("gh")
if not gh:
    print("gh is not installed or not on PATH", flush=True)
    sys.exit(1)

existing = subprocess.run(
    [gh, "pr", "list", "--head", branch, "--state", "open", "--json", "url", "--limit", "1"],
    capture_output=True, text=True,
)
if existing.returncode == 0:
    prs = json.loads(existing.stdout or "[]")
    if prs:
        print(f"Branch {branch} already has an open pull request; its new commits update it.", flush=True)
        print(prs[0]["url"], flush=True)
        sys.exit(0)

result = subprocess.run(
    [gh, "pr", "create", "--base", base, "--head", branch, "--title", title, "--body-file", "-"],
    input=body, text=True, encoding="utf-8",
)
sys.exit(result.returncode)
