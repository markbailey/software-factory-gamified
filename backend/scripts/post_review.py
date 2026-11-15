"""PR poster: post Claude's review (after you approved it at the gate) as your GitHub review.

Usage: post_review.py <pr-url> <head-sha>
stdin: the review, then optionally a line "<<<NOTE>>>" and your note from the game.
The review's first line picks the kind of review:
  "Verdict: Approve" -> approve, "Verdict: Request changes" -> request changes,
  anything else (e.g. "Verdict: Comment") -> a plain comment review.
Your note, if any, goes at the top of the review. If its first line is a verdict line, it
replaces Claude's verdict (type "Verdict: Comment" to post without approving).
The review is pinned to <head-sha>, the commit Claude read. Exit 1 without posting if the PR
has moved on (new commits, closed, or draft): a newer crate reviews the new commits.
"""
import json
import re
import shutil
import subprocess
import sys

URL_RE = re.compile(r"https://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)")
VERDICT_RE = re.compile(r"^\W*verdict\W*\s*(.+?)\W*$", re.IGNORECASE)
EVENTS = {"approve": "APPROVE", "approved": "APPROVE", "looks good": "APPROVE",
          "request changes": "REQUEST_CHANGES", "needs changes": "REQUEST_CHANGES",
          "changes requested": "REQUEST_CHANGES"}
LABELS = {"APPROVE": "an approval", "REQUEST_CHANGES": "a request for changes", "COMMENT": "a comment"}
FOOTER = "\n\n---\n_Drafted with Claude in the Software Factory and approved by me before posting._"


def split_verdict(text: str) -> tuple[str | None, str]:
    """(event, rest) if the first non-blank line is a verdict line, else (None, text)."""
    lines = text.strip().splitlines()
    if lines:
        m = VERDICT_RE.match(lines[0])
        if m:
            return EVENTS.get(m.group(1).strip().lower(), "COMMENT"), "\n".join(lines[1:]).strip()
    return None, text.strip()


def main() -> int:
    url, sha = sys.argv[1], sys.argv[2]
    m = URL_RE.match(url)
    if not m:
        print(f"Not a GitHub pull request URL: {url}", flush=True)
        return 1
    owner, repo, number = m.groups()
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    review, _, note = raw.partition("<<<NOTE>>>")
    event, body = split_verdict(review)
    note_event, note = split_verdict(note)
    event = note_event or event or "COMMENT"
    if note:
        body = f"{note}\n\n{body}".strip()
    if not body and event != "APPROVE":
        print("The review is empty; nothing to post.", flush=True)
        return 1

    gh = shutil.which("gh")
    if not gh:
        print("gh is not installed or not on PATH", flush=True)
        return 1
    r = subprocess.run([gh, "pr", "view", url, "--json", "state,isDraft,headRefOid"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"Could not read the pull request: {(r.stderr or r.stdout).strip()}", flush=True)
        return 1
    pr = json.loads(r.stdout)
    if pr.get("state") != "OPEN" or pr.get("isDraft"):
        print("The pull request is no longer open and ready for review; not posting.", flush=True)
        return 1
    if pr.get("headRefOid") != sha:
        print(f"New commits were pushed after this review ({sha[:7]} -> {str(pr.get('headRefOid'))[:7]}); "
              "not posting. A newer crate reviews the new commits.", flush=True)
        return 1

    payload = json.dumps({"commit_id": sha, "event": event, "body": (body + FOOTER).strip()})
    r = subprocess.run([gh, "api", "-X", "POST", f"repos/{owner}/{repo}/pulls/{number}/reviews",
                        "--input", "-"], input=payload, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"GitHub refused the review: {(r.stderr or r.stdout).strip()}", flush=True)
        return 1
    posted = json.loads(r.stdout or "{}")
    print(f"Posted {LABELS[event]} on {owner}/{repo}#{number}: {posted.get('html_url', url)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
