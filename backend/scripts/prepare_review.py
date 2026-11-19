"""Reviewer, step 2: gather what this review type reads besides the diff.

Usage: prepare_review.py <review-dir> <review-type> <repo> <base-ref> <files> [pr-url]
         [--source S --source-ref R --github-repo O/N --plans DIR --prd-ref N
          --jira-url U --jira-email E --jira-token-env VAR --draft-on-stdin]
With --draft-on-stdin: on stdin, the PR description a Document writer drafted, for a branch with no PR yet.

- security, custom, intent: copies <files> (comma-separated repo paths; wildcards and folders
  like docs/ allowed) from <base-ref> of the local clone <repo> into <review-dir>/reference/.
  They come from the base branch, not the change, so a PR can't rewrite its own rules.
- intent (and the old docs / ticket types it replaced): also saves what says what was asked:
  the crate's own issue (GitHub) or ticket (Jira) and the issues the PR links to ("Closes #12")
  in ticket.md; the approved plan the issue is a task of, and the PRD it came from, in plan.md;
  a drafted PR description into pr.md; and in sources.md, what was found and what wasn't.
Anything missing is reported in the log and in those files for Claude; it never fails the crate
(a review with little to compare against says so, and its confidence shows it).
"""
import argparse
import base64
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

MAX_FILES = 40
MAX_BYTES = 400_000
MAX_TEXT = 60_000  # per issue, plan or PRD
ALIASES = {"docs": "intent", "ticket": "intent"}  # merged into intent (factory/review_types.py)

ap = argparse.ArgumentParser()
for name in ("out", "kind", "repo", "base_ref", "wanted"):
    ap.add_argument(name)
ap.add_argument("pr_url", nargs="?", default="")
ap.add_argument("--draft-on-stdin", action="store_true")
for flag in ("source", "source-ref", "github-repo", "plans", "prd-ref", "jira-url", "jira-email", "jira-token-env"):
    ap.add_argument("--" + flag, default="")
args = ap.parse_args()
out, repo, base_ref, wanted, pr_url = Path(args.out), args.repo, args.base_ref, args.wanted, args.pr_url
kind = ALIASES.get(args.kind, args.kind)
out.mkdir(parents=True, exist_ok=True)
ref_dir = out / "reference"
shutil.rmtree(ref_dir, ignore_errors=True)  # a reworked crate starts clean
for name in ("ticket.md", "plan.md", "sources.md"):
    (out / name).unlink(missing_ok=True)


def run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace")


def patterns(text: str) -> list[str]:
    found = []
    for p in re.split(r"[,\n]", text or ""):
        p = p.strip().replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        if p.lstrip("/"):
            found.append(p.lstrip("/"))
    return found


def matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        return path.startswith(pattern)
    return path == pattern or fnmatch.fnmatchcase(path, pattern) or path.startswith(pattern.rstrip("/") + "/")


def copy_reference() -> None:
    wanted_list = patterns(wanted)
    notes = []
    if not wanted_list:
        notes.append("No files were set on this Reviewer for this review type.")
    else:
        git = shutil.which("git")
        if not git:
            print("git is not installed or not on PATH; can't read the repo's files", flush=True)
            notes.append("The factory could not read the repo (git is missing).")
            wanted_list = []
        tree = run([git, "-C", repo, "ls-tree", "-r", "--name-only", base_ref]) if wanted_list else None
        if tree is not None and tree.returncode != 0 and base_ref.startswith("origin/"):
            run([git, "-C", repo, "fetch", "--quiet", "origin", base_ref.split("/", 1)[1]])
            tree = run([git, "-C", repo, "ls-tree", "-r", "--name-only", base_ref])
        if tree is not None and tree.returncode != 0:
            print(f"Could not list {base_ref} in {repo}: {(tree.stderr or tree.stdout).strip()}", flush=True)
            notes.append(f"The factory could not read {base_ref} of the repo.")
            wanted_list = []
        paths = tree.stdout.splitlines() if tree is not None and tree.returncode == 0 else []
        copied, total = [], 0
        for pattern in wanted_list:
            hits = [p for p in paths if matches(p, pattern)]
            if not hits:
                print(f"Nothing in {base_ref} matches {pattern!r}", flush=True)
                notes.append(f"Nothing on the base branch matches `{pattern}`.")
            for path in hits:
                if path in copied:
                    continue
                if len(copied) >= MAX_FILES or total >= MAX_BYTES:
                    notes.append(f"Stopped at {len(copied)} files ({total // 1000} KB); the rest were left out.")
                    break
                shown = subprocess.run([git, "-C", repo, "show", f"{base_ref}:{path}"], capture_output=True)
                if shown.returncode != 0 or b"\0" in shown.stdout[:4000]:
                    continue  # unreadable or binary
                target = ref_dir / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(shown.stdout[:MAX_BYTES - total])
                copied.append(path)
                total += min(len(shown.stdout), MAX_BYTES - total)
            else:
                continue
            break
        print(f"Copied {len(copied)} reference file(s) from {base_ref}: " + (", ".join(copied) or "none"), flush=True)
        if copied:
            notes.insert(0, "Files copied from the base branch (" + base_ref + "):\n"
                         + "\n".join(f"- {p}" for p in copied))
    ref_dir.mkdir(parents=True, exist_ok=True)
    (ref_dir / "README.md").write_text("\n\n".join(notes) + "\n", encoding="utf-8")


def gh_json(argv: list[str]) -> dict | None:
    gh = shutil.which("gh")
    if not gh:
        return None
    r = run([gh, *argv])
    if r.returncode != 0:
        print(f"gh {' '.join(argv[:3])} failed: {(r.stderr or r.stdout).strip()}", flush=True)
        return None
    try:
        return json.loads(r.stdout or "{}")
    except ValueError:
        return None


def issue(ref: str) -> dict | None:
    """A GitHub issue by number, "owner/repo#12" or URL: number, title, body, url."""
    arg, extra = ref, ["--repo", args.github_repo] if args.github_repo else []
    if "#" in ref and not ref.startswith("http"):
        owner_repo, arg = ref.split("#", 1)
        extra = ["--repo", owner_repo] if owner_repo else extra
    return gh_json(["issue", "view", arg, *extra, "--json", "number,title,body,url"])


def issue_md(i: dict, heading: str = "") -> str:
    body = (i.get("body") or "(no description)").strip()[:MAX_TEXT]
    return f"## {heading}#{i.get('number')}: {i.get('title', '')}\n{i.get('url', '')}\n\n{body}\n"


def jira_ticket(key: str) -> str:
    token = os.environ.get(args.jira_token_env or "JIRA_API_TOKEN", "")
    if not args.jira_url or not token:
        print(f"Jira isn't set up here, so the ticket {key} can't be read.", flush=True)
        return ""
    auth = base64.b64encode(f"{args.jira_email}:{token}".encode()).decode()
    req = urllib.request.Request(f"{args.jira_url.rstrip('/')}/rest/api/2/issue/{key}?fields=summary,description",
                                 headers={"Authorization": f"Basic {auth}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            fields = json.loads(resp.read() or b"{}").get("fields") or {}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"Could not read the Jira ticket {key}: {exc}", flush=True)
        return ""
    body = str(fields.get("description") or "(no description)").strip()[:MAX_TEXT]
    return (f"## The ticket this work came from: {key}: {fields.get('summary', '')}\n"
            f"{args.jira_url.rstrip('/')}/browse/{key}\n\n{body}\n")


def linked_refs() -> list[str]:
    if not pr_url:
        return []
    pr = gh_json(["pr", "view", pr_url, "--json", "body,closingIssuesReferences"]) or \
        gh_json(["pr", "view", pr_url, "--json", "body"]) or {}
    refs = [str(i.get("url") or i.get("number")) for i in pr.get("closingIssuesReferences") or [] if i]
    if not refs:  # older gh: the "Closes #12" lines in the description
        refs = [m.group(1) for m in re.finditer(
            r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+((?:[\w.-]+/[\w.-]+)?#\d+)", pr.get("body") or "", re.I)]
    return refs


def plan_record(number: int) -> tuple[dict, dict] | None:
    """The Issue maker's record of the plan that has this issue as a task, and the task."""
    folder = Path(args.plans) if args.plans else None
    if not folder or not folder.is_dir():
        return None
    for path in sorted(folder.glob("crate-*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if args.github_repo and record.get("repo") and record["repo"].lower() != args.github_repo.lower():
            continue
        for key, task in (record.get("tasks") or {}).items():
            if task.get("number") == number:
                return record, {"key": key, **task}
    return None


def gather_intent() -> None:
    found, missing = [], []
    tickets, seen = [], set()
    source, ref = args.source, args.source_ref.strip()

    # The crate's own issue or ticket.
    if source in ("github", "prd") and ref:
        i = issue(ref)
        if i:
            seen.add(str(i.get("number")))
            what = "The PRD this work came from: " if source == "prd" else "The issue this work came from: "
            tickets.append(issue_md(i, what))
            found.append(f"the issue the crate came from (#{i.get('number')}), in ticket.md")
        else:
            missing.append(f"the issue the crate came from (#{ref}) could not be read")
    elif source == "jira" and ref:
        text = jira_ticket(ref)
        if text:
            tickets.append(text)
            found.append(f"the Jira ticket the crate came from ({ref}), in ticket.md")
        else:
            missing.append(f"the Jira ticket the crate came from ({ref}) could not be read")
    elif source in ("", "game"):
        missing.append("no issue or ticket: the crate was typed in the game, so its spec is the request")

    # The issues the PR links to.
    if pr_url:
        linked = 0
        for r in linked_refs():
            num = r.rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            if num in seen or r in seen:
                continue
            seen.update((num, r))
            i = issue(r)
            if i:
                tickets.append(issue_md(i, "An issue the PR links to: "))
                linked += 1
        found.append(f"the PR's description, in pr.md; {linked} more issue(s) it links to" if linked
                     else "the PR's description, in pr.md (it links no other issues)")
    else:
        missing.append("no pull request yet: pr.md has the branch's commits instead of a description")
        draft = sys.stdin.read().strip() if args.draft_on_stdin else ""
        if draft and (out / "pr.md").exists():
            with (out / "pr.md").open("a", encoding="utf-8") as f:
                f.write(f"\n## Description (drafted for the PR, not opened yet)\n\n{draft[:MAX_TEXT]}\n")
            found.append("the drafted PR description, in pr.md")

    if tickets:
        (out / "ticket.md").write_text("# What was asked: issues and tickets (written by their authors)\n\n"
                                       + "\n".join(tickets), encoding="utf-8")

    # The plan the issue is a task of, and the PRD the plan came from.
    plan_parts = []
    hit = plan_record(int(ref)) if source == "github" and ref.isdigit() else None
    prd = args.prd_ref.strip().lstrip("#")
    if hit:
        record, task = hit
        prd = prd or str(record.get("prd") or "")
        plan_parts.append(f"This change is task {task['key']} of the plan \"{record.get('title', '')}\": "
                          f"#{task.get('number')} {task.get('title', '')}.\n")
        if record.get("tracking"):
            t = issue(str(record["tracking"]))
            if t:
                body = t.get("body") or ""
                m = re.search(r"Planned by the Software Factory from #(\d+)", body)
                prd = prd or (m.group(1) if m else "")
                plan_parts.append(issue_md(t, "The approved plan (its tracking issue) "))
                found.append(f"the approved plan (#{record['tracking']}) this issue is a task of, in plan.md")
            else:
                missing.append(f"the plan's tracking issue #{record['tracking']} could not be read")
        else:
            found.append("the plan this issue is part of (a one-issue plan: the issue is the whole plan)")
    elif source == "github":
        missing.append("no plan: the issue wasn't made by the factory's Planner")
    if prd and prd not in seen:
        p = issue(prd)
        if p:
            plan_parts.append(issue_md(p, "The PRD the plan came from "))
            found.append(f"the PRD (#{prd}), in plan.md")
        else:
            missing.append(f"the PRD #{prd} could not be read")
    if plan_parts:
        (out / "plan.md").write_text("# The plan and PRD this work belongs to (written by people; the plan as "
                                     "approved)\n\n" + "\n".join(plan_parts), encoding="utf-8")

    docs = [p for p in patterns(wanted)]
    (found if docs else missing).append("documentation in reference/ (see reference/README.md)" if docs
                                        else "no documentation was set on this Reviewer")
    (out / "sources.md").write_text(
        "# What the review has to check against\n\nFound:\n" + "".join(f"- {f}\n" for f in found or ["nothing"])
        + "\nNot available:\n" + "".join(f"- {m}\n" for m in missing or ["nothing missing"]), encoding="utf-8")
    for line in found:
        print(f"Found {line}", flush=True)
    for line in missing:
        print(f"Not available: {line}", flush=True)


if kind in ("security", "custom"):
    copy_reference()
elif kind == "intent":
    if patterns(wanted):
        copy_reference()
    gather_intent()
else:
    print(f"Nothing extra to gather for a {kind or 'general'} review.", flush=True)
