"""Merger: wait until GitHub lets the pull request merge, then merge it.

Usage: merge_when_ready.py <pr-url> [timeout-seconds, 0 = wait for ever] [squash|merge|rebase]
The last is how it merges: one squashed commit (the default), a merge commit, or the PR's
commits rebased onto the base branch.

While GitHub's branch rules hold the PR back (an approving review, required checks, a branch
that is behind its base), this prints "WAITING <why>" and keeps checking: the crate shows as
waiting on its machine instead of failing. It also turns on GitHub auto-merge where the repo
allows it, so GitHub merges the PR the moment it's allowed even if the factory is closed.
A branch that is behind its base is brought up to date once.

Exit 0 when the PR is merged (by us, by auto-merge, or by someone else).
Exit 1 when it can't be merged without the Coder: merge conflicts, a reviewer asked for
changes (their review is printed), or a required check failed. Auto-merge is turned off first,
so GitHub can't merge the PR while it's being fixed. The Merger sends the crate back to the
Coder with this output. A closed PR prints NO_REWORK: rework can't fix that, so it's scrapped.
"""
import json
import shutil
import subprocess
import sys
import time

url = sys.argv[1]
timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 0
METHOD = "--" + (sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] in ("squash", "merge", "rebase") else "squash")
POLL = 30
GH_RETRIES = 4  # a few failed lookups in a row (GitHub hiccups) before giving up

gh = shutil.which("gh")
if not gh:
    print("gh is not installed or not on PATH", flush=True)
    sys.exit(1)


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([gh, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


def say(text: str) -> None:
    print(text, flush=True)


def view() -> dict | None:
    r = run("pr", "view", url, "--json",
            "state,isDraft,mergeable,mergeStateStatus,reviewDecision,autoMergeRequest,statusCheckRollup")
    if r.returncode != 0:
        say(f"gh pr view failed: {(r.stderr or r.stdout).strip()}")
        return None
    return json.loads(r.stdout)


def stop(reason: str, rework: bool = True) -> None:
    """Fail: turn auto-merge off (so nothing half-fixed merges), say why, and exit 1."""
    if auto_on and rework:
        r = run("pr", "merge", url, "--disable-auto")
        if r.returncode == 0:
            say("Turned auto-merge off until it's fixed.")
    if waiting:
        say("WAITING")
    say(reason)
    if not rework:
        say("NO_REWORK")
    sys.exit(1)


def change_requests() -> tuple[str, bool]:
    """The change requests' text, and whether they're newer than the last commit (so not yet
    answered: after the Coder pushes a fix it waits for the reviewer to look again)."""
    r = run("pr", "view", url, "--json", "latestReviews,commits")
    if r.returncode != 0:
        return "", True
    data = json.loads(r.stdout)
    reviews = [x for x in data.get("latestReviews") or [] if x.get("state") == "CHANGES_REQUESTED"]
    commits = data.get("commits") or []
    last_commit = str(commits[-1].get("committedDate", "")) if commits else ""
    fresh = any(str(x.get("submittedAt", "")) > last_commit for x in reviews) if last_commit else True
    return "\n\n".join(f"From {(x.get('author') or {}).get('login', 'a reviewer')}:\n{(x.get('body') or '').strip()}"
                       for x in reviews), fresh


def pending_checks(pr: dict) -> list[str]:
    names = []
    for c in pr.get("statusCheckRollup") or []:
        done = c.get("status") == "COMPLETED" or c.get("state") in ("SUCCESS", "FAILURE", "ERROR")
        if not done:
            names.append(c.get("name") or c.get("context") or "a check")
    return names


def failed_checks(pr: dict) -> list[str]:
    bad = ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED")
    return [c.get("name") or c.get("context") or "a check" for c in pr.get("statusCheckRollup") or []
            if c.get("conclusion") in bad or c.get("state") in ("FAILURE", "ERROR")]


start = time.monotonic()
failures = 0
waiting = ""
tried_auto = False
auto_on = False  # auto-merge is on for this PR (we turned it on, or someone did)
updated = False
while True:
    pr = view()
    if pr is None:
        failures += 1
        if failures >= GH_RETRIES:
            sys.exit(1)
        time.sleep(POLL)
        continue
    failures = 0
    state = pr.get("state")
    auto_on = bool(pr.get("autoMergeRequest")) or auto_on
    status = pr.get("mergeStateStatus") or "UNKNOWN"

    if state == "MERGED":
        if waiting:
            say("WAITING")
        say("The pull request is merged.")
        sys.exit(0)
    if state == "CLOSED":
        stop("The pull request was closed without merging, so there is nothing to merge.", rework=False)
    if pr.get("mergeable") == "CONFLICTING" or status == "DIRTY":
        stop("The pull request has merge conflicts with its base branch. The factory merges the base "
             "branch into this branch before you start (don't run git yourself): resolve the conflict "
             "markers it leaves, keeping what both sides meant, and make sure the tests still pass.")
    reviewer_waiting = False
    if pr.get("reviewDecision") == "CHANGES_REQUESTED":
        text, fresh = change_requests()
        if fresh:
            stop("A reviewer requested changes on GitHub. Make them:\n\n" + (text or "(no text)"))
        reviewer_waiting = True  # fixed since: wait for them to look again
    if pr.get("isDraft"):
        r = run("pr", "ready", url)
        say("Marked the draft pull request ready for review." if r.returncode == 0
            else f"Couldn't mark the draft ready: {(r.stderr or r.stdout).strip()}")

    why = ""
    if status in ("CLEAN", "HAS_HOOKS", "UNSTABLE"):
        r = run("pr", "merge", url, METHOD)
        out = (r.stdout + r.stderr).strip()
        if r.returncode == 0:
            if waiting:
                say("WAITING")
            say(out or "Merged.")
            sys.exit(0)
        why = "GitHub didn't allow the merge yet"
        say(f"Merge refused, will try again: {out}")
    elif status == "BEHIND":
        if not updated:
            updated = True
            r = run("api", "-X", "PUT", url.replace("https://github.com/", "repos/").replace("/pull/", "/pulls/")
                    + "/update-branch")
            say("Brought the branch up to date with its base." if r.returncode == 0
                else f"Couldn't update the branch: {(r.stderr or r.stdout).strip()}")
        why = "Waiting for the branch to be brought up to date with its base"
    elif status == "BLOCKED":
        checks = pending_checks(pr)
        bad = failed_checks(pr)
        if bad:
            stop(f"Required checks failed: {', '.join(bad)}. See the checks on the pull request.")
        if reviewer_waiting:
            why = "Waiting for the reviewer to look at the fix on GitHub"
        elif pr.get("reviewDecision") == "REVIEW_REQUIRED":
            why = "Waiting for an approving review on GitHub"
        elif checks:
            why = f"Waiting for checks: {', '.join(checks[:3])}" + (" and more" if len(checks) > 3 else "")
        else:
            why = "Waiting for GitHub's branch rules (a required review or check)"
    else:  # UNKNOWN: GitHub is still working out whether it can merge
        why = "Waiting for GitHub to check whether it can merge"

    if why != waiting:
        waiting = why
        say(f"WAITING {why}")
        if not tried_auto and status in ("BLOCKED", "BEHIND") and not pr.get("autoMergeRequest"):
            tried_auto = True
            r = run("pr", "merge", url, METHOD, "--auto")
            auto_on = r.returncode == 0
            say("Turned on auto-merge: GitHub merges it as soon as it's allowed." if auto_on
                else "Auto-merge isn't available on this repo; the factory keeps checking instead.")

    if timeout and time.monotonic() - start > timeout:
        stop(f"Gave up waiting after {timeout:.0f}s: {why}.", rework=False)
    time.sleep(POLL)
