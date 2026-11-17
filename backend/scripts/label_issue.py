"""Labeller: add and remove labels on the crate's issue (or its PR, for open PRs checked by a
line), on GitHub or in Jira.

    label_issue.py SOURCE REF GITHUB_REPO "ADD" "REMOVE" [JIRA_URL JIRA_EMAIL JIRA_TOKEN_ENV]

ADD and REMOVE are comma-separated labels (either may be empty). GitHub labels that don't exist
yet are created. Jira labels can't hold spaces, so spaces become dashes there. A crate that
didn't come from an issue (typed in the game, a PR) has nothing to label and passes.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request


def labels(text: str) -> list[str]:
    return [l.strip() for l in (text or "").split(",") if l.strip()]


def gh(*args: str) -> subprocess.CompletedProcess:
    exe = shutil.which("gh")
    if not exe:
        print("gh is not installed or not on PATH", flush=True)
        sys.exit(2)
    return subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


def label_github(ref: str, repo: str, add: list[str], remove: list[str], kind: str = "issue") -> None:
    number = ref.lstrip("#")
    failed = []
    for label in add:
        gh("label", "create", label, "--repo", repo, "--color", "C5DEF5", "--force")  # fine if it exists
        r = gh(kind, "edit", number, "--repo", repo, "--add-label", label)
        print(f"added '{label}'" if r.returncode == 0 else f"could not add '{label}': {r.stderr.strip()}", flush=True)
        if r.returncode != 0:
            failed.append(r.stderr)
    for label in remove:
        r = gh(kind, "edit", number, "--repo", repo, "--remove-label", label)
        # Removing a label the issue doesn't have isn't a failure.
        print(f"removed '{label}'" if r.returncode == 0 else f"'{label}' wasn't on it", flush=True)
    if failed:
        print("\n".join(failed), flush=True)
        sys.exit(1)


def label_jira(key: str, base_url: str, email: str, token_env: str, add: list[str], remove: list[str]) -> None:
    token = os.environ.get(token_env or "JIRA_API_TOKEN", "")
    if not base_url or not token:
        print(f"Jira isn't set up here (needs [intake.jira] base_url and the {token_env} variable).", flush=True)
        sys.exit(1)
    fix = lambda l: l.replace(" ", "-")  # noqa: E731
    update = [{"add": fix(l)} for l in add] + [{"remove": fix(l)} for l in remove]
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    req = urllib.request.Request(f"{base_url.rstrip('/')}/rest/api/3/issue/{key}", method="PUT",
                                 data=json.dumps({"update": {"labels": update}}).encode(),
                                 headers={"Authorization": f"Basic {auth}", "Accept": "application/json",
                                          "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as e:
        print(f"Jira said {e.code} when labelling {key}: {e.read().decode(errors='replace')[:500]}", flush=True)
        sys.exit(1)
    for l in add:
        print(f"added '{fix(l)}'", flush=True)
    for l in remove:
        print(f"removed '{fix(l)}'", flush=True)


def main() -> None:
    args = sys.argv[1:] + [""] * 8
    source, ref, repo, add_text, remove_text, jira_url, jira_email, jira_token_env = args[:8]
    add, remove = labels(add_text), labels(remove_text)
    if not add and not remove:
        print("No labels set in the Labeller's settings: nothing to do.", flush=True)
        return
    if source in ("github", "github_pr") and ref:
        if not repo:
            print("No GitHub repo for this line.", flush=True)
            sys.exit(1)
        kind = "pr" if source == "github_pr" else "issue"
        print(f"GitHub {'PR' if kind == 'pr' else 'issue'} {repo}#{ref.lstrip('#')}:", flush=True)
        label_github(ref, repo, add, remove, kind)
    elif source == "jira" and ref:
        print(f"Jira issue {ref}:", flush=True)
        label_jira(ref, jira_url, jira_email, jira_token_env, add, remove)
    else:
        print(f"This crate came from {source or 'the game'}, not an issue tracker: nothing to label.", flush=True)


if __name__ == "__main__":
    main()
