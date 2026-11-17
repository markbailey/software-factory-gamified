"""Verifier: is the crate's issue still open, still wanted, and free to work on?

    verify_issue.py SOURCE REF GITHUB_REPO [JIRA_URL JIRA_EMAIL JIRA_TOKEN_ENV [STALE_DAYS]]

Looks the issue (or PR) up again at its source (it may have changed since it came in):
  GitHub  closed (as done, not planned or a duplicate), labelled duplicate / wontfix / invalid
          / blocked / needs info, or "depends on #n" / "blocked by #n" an issue that's still open.
  Jira    in a Done status, resolved, or linked "is blocked by" an issue that isn't done.
  PR      (source github_pr) merged or closed, every issue it closes already closed, labelled
          as above, or untouched for STALE_DAYS days (0 or empty: never stale).
Crates typed in the game or PRs sent for review have nothing to look up, so they pass.

Passing prints what it checked and exits 0. Otherwise prints OUTCOME <needs_info |
already_done | blocked>, NO_REWORK and why, and exits 1: the crate leaves by the red port and
its issue is handed back with the reason.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

# Labels that say the issue shouldn't be worked on (any case; spaces, - and _ alike).
LABEL_OUTCOMES = {
    "duplicate": ("already_done", "it's labelled duplicate"),
    "wontfix": ("already_done", "it's labelled wontfix"),
    "wont fix": ("already_done", "it's labelled won't fix"),
    "invalid": ("already_done", "it's labelled invalid"),
    "blocked": ("blocked", "it's labelled blocked"),
    "on hold": ("blocked", "it's labelled on hold"),
    "needs info": ("needs_info", "it's labelled needs info"),
    "needs more info": ("needs_info", "it's labelled needs more info"),
    "question": ("needs_info", "it's labelled question"),
}
# "depends on #12", "blocked by owner/repo#3", "after #4", "requires #5" (also "Depends on: #6, #7").
DEPENDS_RE = re.compile(r"\b(?:depends\s+on|blocked\s+by|waiting\s+on|requires|after)\s*:?\s*"
                        r"((?:[\w.-]+/[\w.-]+)?#\d+(?:\s*(?:,|and)\s*(?:[\w.-]+/[\w.-]+)?#\d+)*)", re.I)
REF_RE = re.compile(r"(?:([\w.-]+/[\w.-]+))?#(\d+)")


def fail(kind: str, why: str) -> None:
    print(f"OUTCOME {kind}", flush=True)
    print("NO_REWORK", flush=True)
    print(f"Not ready to work on: {why}", flush=True)
    sys.exit(1)


def label_key(name: str) -> str:
    return re.sub(r"[\s_-]+", " ", name.strip().lower()).replace("won't", "wont")


def gh_json(*args: str) -> dict:
    gh = shutil.which("gh")
    if not gh:
        print("gh is not installed or not on PATH", flush=True)
        sys.exit(2)
    out = subprocess.run([gh, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        print(out.stderr.strip() or out.stdout.strip(), flush=True)
        sys.exit(2)
    return json.loads(out.stdout or "{}")


def check_github(ref: str, repo: str) -> None:
    number = ref.lstrip("#")
    fields = "number,title,state,stateReason,labels,body,url"
    issue = gh_json("issue", "view", number, "--repo", repo, "--json", fields)
    print(f"GitHub issue #{number}: {issue.get('title', '')}", flush=True)
    if str(issue.get("state", "")).upper() == "CLOSED":
        reason = str(issue.get("stateReason") or "").upper()
        words = {"COMPLETED": "it's closed as completed", "NOT_PLANNED": "it's closed as not planned",
                 "DUPLICATE": "it's closed as a duplicate"}.get(reason, "it's closed")
        fail("already_done", words)
    for label in issue.get("labels", []) or []:
        hit = LABEL_OUTCOMES.get(label_key(str(label.get("name", ""))))
        if hit:
            fail(*hit)
    waits = []
    for group in DEPENDS_RE.findall(str(issue.get("body") or "")):
        for other_repo, other in REF_RE.findall(group):
            if other == number and not other_repo:
                continue
            dep = gh_json("issue", "view", other, "--repo", other_repo or repo, "--json", "state,title")
            state = str(dep.get("state", "")).upper()
            print(f"  depends on {other_repo}#{other} ({dep.get('title', '')}): {state.lower()}", flush=True)
            if state != "CLOSED":
                waits.append(f"{other_repo}#{other}")
    if waits:
        fail("blocked", f"it waits on {', '.join(waits)}, still open")
    print("Open, not labelled as blocked or a duplicate, and nothing it depends on is still open.", flush=True)


def check_pr(ref: str, repo: str, stale_days: str) -> None:
    number = ref.lstrip("#")
    fields = "number,title,state,isDraft,labels,updatedAt,closingIssuesReferences,url"
    pr = gh_json("pr", "view", number, "--repo", repo, "--json", fields)
    print(f"GitHub PR #{number}: {pr.get('title', '')}", flush=True)
    state = str(pr.get("state", "")).upper()
    if state == "MERGED":
        fail("already_done", "it's merged")
    if state == "CLOSED":
        fail("already_done", "it's closed")
    for label in pr.get("labels", []) or []:
        hit = LABEL_OUTCOMES.get(label_key(str(label.get("name", ""))))
        if hit:
            fail(*hit)
    fixes = pr.get("closingIssuesReferences") or []
    if fixes:
        still_open = []
        for issue in fixes:
            owner = ((issue.get("repository") or {}).get("owner") or {}).get("login", "")
            name = (issue.get("repository") or {}).get("name", "")
            where = f"{owner}/{name}" if owner and name else repo
            other = gh_json("issue", "view", str(issue.get("number")), "--repo", where, "--json", "state,title")
            done = str(other.get("state", "")).upper() == "CLOSED"
            print(f"  closes #{issue.get('number')} ({other.get('title', '')}): {'closed' if done else 'open'}",
                  flush=True)
            if not done:
                still_open.append(issue)
        if not still_open:
            fail("already_done", "every issue it was going to close is closed already, so it isn't needed")
    days = int(stale_days) if str(stale_days).strip().isdigit() else 0
    updated = str(pr.get("updatedAt") or "")
    if days and updated:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except ValueError:
            age = None
        if age is not None and age.days >= days:
            fail("already_done", f"nobody has touched it for {age.days} days (stale after {days})")
    print("Open, not labelled as blocked or a duplicate, its issues still open"
          + (f", and touched in the last {days} days." if days else "."), flush=True)


def check_jira(key: str, base_url: str, email: str, token_env: str) -> None:
    token = os.environ.get(token_env or "JIRA_API_TOKEN", "")
    if not base_url or not token:
        print(f"Jira isn't set up here (needs [intake.jira] base_url and the {token_env} variable); "
              "nothing to check at the source.", flush=True)
        return
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    req = urllib.request.Request(f"{base_url.rstrip('/')}/rest/api/3/issue/{key}?fields=summary,status,"
                                 "resolution,issuelinks,labels",
                                 headers={"Authorization": f"Basic {auth}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            issue = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            fail("already_done", f"{key} no longer exists in Jira")
        print(f"Jira said {e.code} for {key}: {e.reason}", flush=True)
        sys.exit(2)
    fields = issue.get("fields", {})
    print(f"Jira issue {key}: {fields.get('summary', '')}", flush=True)
    status = fields.get("status") or {}
    if (status.get("statusCategory") or {}).get("key") == "done":
        fail("already_done", f"it's {status.get('name', 'done')} in Jira")
    if fields.get("resolution"):
        fail("already_done", f"it's resolved in Jira ({fields['resolution'].get('name', '')})")
    for label in fields.get("labels", []) or []:
        hit = LABEL_OUTCOMES.get(label_key(str(label)))
        if hit:
            fail(*hit)
    waits = []
    for link in fields.get("issuelinks", []) or []:
        inward = str((link.get("type") or {}).get("inward", "")).lower()
        other = link.get("inwardIssue")
        if other and "blocked by" in inward:
            category = (((other.get("fields") or {}).get("status") or {}).get("statusCategory") or {}).get("key")
            print(f"  is blocked by {other.get('key')}: {category or 'unknown'}", flush=True)
            if category != "done":
                waits.append(str(other.get("key")))
    if waits:
        fail("blocked", f"it's blocked by {', '.join(waits)}, not done yet")
    print("Not done, not resolved, and nothing blocking it is still open.", flush=True)


def main() -> None:
    args = sys.argv[1:] + [""] * 7
    source, ref, repo, jira_url, jira_email, jira_token_env, stale_days = args[:7]
    if source == "github_pr" and ref:
        if not repo:
            print("No GitHub repo for this line; nothing to check at the source.", flush=True)
            return
        check_pr(ref, repo, stale_days)
    elif source == "github" and ref:
        if not repo:
            print("No GitHub repo for this line; nothing to check at the source.", flush=True)
            return
        check_github(ref, repo)
    elif source == "jira" and ref:
        check_jira(ref, jira_url, jira_email, jira_token_env)
    else:
        print(f"This crate came from {source or 'the game'}, not an issue tracker: nothing to check at the source.",
              flush=True)


if __name__ == "__main__":
    main()
