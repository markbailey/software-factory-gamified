"""Packager: open a pull request for the current branch, or reuse the one already open.

Usage: open_pr.py <base> <branch> <title> [reviewers]   (PR body on stdin)
Prints the PR URL. A retried crate pushes to the same branch, so its open PR updates itself.
reviewers: GitHub usernames or org/team names, separated by commas or spaces, asked to review
the PR (also on a PR that's already open). One that can't be asked is reported, not a failure.
"""
import json
import re
import shutil
import subprocess
import sys

base, branch, title = sys.argv[1], sys.argv[2], sys.argv[3]
reviewers = [r.strip().lstrip("@") for r in re.split(r"[,\s]+", sys.argv[4] if len(sys.argv) > 4 else "")
             if r.strip()]
# Read the body as UTF-8 bytes: on Windows the default stdin encoding is cp1252,
# which mangles characters such as curly quotes.
body = sys.stdin.buffer.read().decode("utf-8", errors="replace")
gh = shutil.which("gh")
if not gh:
    print("gh is not installed or not on PATH", flush=True)
    sys.exit(1)


def ask_for_reviews(url: str) -> None:
    """Request a review from each reviewer, one at a time so one bad name can't stop the rest."""
    for who in reviewers:
        r = subprocess.run([gh, "pr", "edit", url, "--add-reviewer", who], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode == 0:
            print(f"Asked {who} to review it.", flush=True)
        else:
            why = (r.stderr or r.stdout).strip().splitlines()
            print(f"Couldn't ask {who} to review it: {why[-1] if why else 'gh failed'}", flush=True)


existing = subprocess.run(
    [gh, "pr", "list", "--head", branch, "--state", "open", "--json", "url", "--limit", "1"],
    capture_output=True, text=True,
)
prs = json.loads(existing.stdout or "[]") if existing.returncode == 0 else []
if prs:
    print(f"Branch {branch} already has an open pull request; its new commits update it.", flush=True)
    print(prs[0]["url"], flush=True)
    ask_for_reviews(prs[0]["url"])
    sys.exit(0)

result = subprocess.run(
    [gh, "pr", "create", "--base", base, "--head", branch, "--title", title, "--body-file", "-"],
    input=body, text=True, encoding="utf-8", capture_output=True,
)
sys.stdout.write(result.stdout)
sys.stderr.write(result.stderr)
sys.stdout.flush()
if result.returncode == 0:
    urls = re.findall(r"https://github\.com/\S+/pull/\d+", result.stdout)
    if urls:
        ask_for_reviews(urls[-1])
sys.exit(result.returncode)
