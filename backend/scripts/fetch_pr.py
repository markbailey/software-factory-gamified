"""Reviewer, step 1: save a pull request and its diff for Claude to read. No checkout needed.

Usage: fetch_pr.py <pr-url> <out-dir> [expected-head-sha]
Writes <out-dir>/pr.md (title, author, branches, description, changed files) and
<out-dir>/diff.patch. Exit 1 if the PR is closed, or (given expected-head-sha: someone else's PR) back in draft or
its head is no longer expected-head-sha (a newer crate reviews the new commits).
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

url, out = sys.argv[1], Path(sys.argv[2])
expected = sys.argv[3] if len(sys.argv) > 3 else ""
gh = shutil.which("gh")
if not gh:
    print("gh is not installed or not on PATH", flush=True)
    sys.exit(1)


def run(*args: str) -> str:
    r = subprocess.run([gh, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"gh {args[0]} {args[1]} failed: {(r.stderr or r.stdout).strip()}", flush=True)
        sys.exit(1)
    return r.stdout


pr = json.loads(run("pr", "view", url, "--json",
                    "number,title,body,author,state,isDraft,baseRefName,headRefName,headRefOid,"
                    "additions,deletions,changedFiles,files"))
if pr.get("state") != "OPEN":
    print(f"The pull request is {str(pr.get('state', '')).lower()}; nothing to review.", flush=True)
    sys.exit(1)
if pr.get("isDraft") and expected:  # someone else's PR; the factory's own stay drafts until merging
    print("The pull request went back to draft; not reviewing it.", flush=True)
    sys.exit(1)
if expected and pr.get("headRefOid") != expected:
    print(f"New commits were pushed since this crate was made ({expected[:7]} -> "
          f"{str(pr.get('headRefOid'))[:7]}); a newer crate reviews them.", flush=True)
    sys.exit(1)

diff = run("pr", "diff", url, "--patch")
out.mkdir(parents=True, exist_ok=True)
files = "\n".join(f"- {f['path']} (+{f.get('additions', 0)} -{f.get('deletions', 0)})"
                  for f in pr.get("files", []) or [])
author = (pr.get("author") or {}).get("login", "unknown")
(out / "pr.md").write_text(
    f"# {pr['title']}\n\n"
    f"Pull request #{pr['number']} by @{author}: {pr['headRefName']} into {pr['baseRefName']}, "
    f"head {pr['headRefOid']}\n"
    f"{pr.get('changedFiles', 0)} files changed, +{pr.get('additions', 0)} -{pr.get('deletions', 0)}\n\n"
    f"## Description (written by the PR author)\n\n{(pr.get('body') or '(none)').strip()}\n\n"
    f"## Files changed\n\n{files or '(none)'}\n",
    encoding="utf-8")
(out / "diff.patch").write_text(diff, encoding="utf-8")
print(f"Saved #{pr['number']} \"{pr['title']}\" by @{author}: {pr.get('changedFiles', 0)} files, "
      f"+{pr.get('additions', 0)} -{pr.get('deletions', 0)}, {len(diff.splitlines())} diff lines", flush=True)
