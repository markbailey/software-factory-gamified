"""Document writer, set to Jira issues: create the plan you approved as issues in a Jira project.

stdin: the Planner's reply (Markdown, then the ```json block with the issues), or the one the
Document writer wrote itself from a crate's description.

Creates one Jira issue per task in --project, of --type (Task by default). A task that waits
for others gets an "is blocked by" link to each of them. Tasks that wait for nothing get
--label (the factory's Jira trigger label) straight away, so a line whose JQL looks for it
picks them up; the backend labels the rest as the ones they wait for ship (it reads the record
this writes under --plans, the same one create_issues.py writes for GitHub, with repo
"jira:<PROJECT>" and the issue keys as numbers).

Safe to run again: what it already created is recorded in <plans>/crate-<id>.json and not
created twice.

The Jira site and email come as arguments, the token in the environment variable --token-env
(the crate's Jira account: its storage's, else its line's, else the default).
"""
import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from plan_format import split

ap = argparse.ArgumentParser()
ap.add_argument("--project", default="")
ap.add_argument("--type", default="Task")
ap.add_argument("--label", default="")
ap.add_argument("--site", default="")
ap.add_argument("--email", default="")
ap.add_argument("--token-env", default="FACTORY_JIRA_TOKEN")
ap.add_argument("--plans", required=True)
ap.add_argument("--crate", required=True)
args = ap.parse_args()

project = args.project.strip().upper()
issue_type = args.type.strip() or "Task"
label = args.label.strip().replace(" ", "-")  # Jira labels can't hold spaces
site = args.site.strip().rstrip("/")
token = os.environ.get(args.token_env or "FACTORY_JIRA_TOKEN", "")

if not project:
    print("No Jira project to create the issues in: set \"Jira project key\" in this machine's settings "
          "(e.g. SF).", flush=True)
    sys.exit(1)
missing = [w for w, v in (("the Jira site", site), ("the email", args.email), ("the API token", token)) if not v]
if missing:
    print("Jira isn't set up for this line: add " + ", ".join(missing) + " in Accounts (the gear in the "
          "top bar), or pick another Jira account for the line or its storage.", flush=True)
    sys.exit(1)

AUTH = base64.b64encode(f"{args.email}:{token}".encode()).decode()


def jira(method: str, path: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(site + path, method=method,
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 headers={"Authorization": f"Basic {AUTH}", "Accept": "application/json",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        print(f"Jira said {e.code} to {method} {path}: {e.read().decode(errors='replace')[:800]}", flush=True)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"Couldn't reach Jira at {site}: {e.reason}", flush=True)
        sys.exit(1)
    return json.loads(raw) if raw else {}


# --- Markdown to Atlassian Document Format (headings, bullet and task lists, code, paragraphs) ---

def _text(s: str) -> list[dict]:
    """Inline text, with `code` and **bold** marked."""
    out: list[dict] = []
    for part in re.split(r"(`[^`]+`|\*\*[^*]+\*\*)", s):
        if not part:
            continue
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append({"type": "text", "text": part[1:-1], "marks": [{"type": "code"}]})
        elif part.startswith("**") and part.endswith("**") and len(part) > 3:
            out.append({"type": "text", "text": part[2:-2], "marks": [{"type": "strong"}]})
        else:
            out.append({"type": "text", "text": part})
    return out


def adf(markdown: str) -> dict:
    content: list[dict] = []
    para: list[str] = []
    bullets: list[dict] = []
    lines = markdown.replace("\r\n", "\n").split("\n")

    def flush() -> None:
        if para:
            content.append({"type": "paragraph", "content": _text(" ".join(para))})
            para.clear()
        if bullets:
            content.append({"type": "bulletList", "content": list(bullets)})
            bullets.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            lang = stripped[3:].strip()
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            block = {"type": "codeBlock", "content": [{"type": "text", "text": "\n".join(code) or " "}]}
            if lang:
                block["attrs"] = {"language": lang}
            content.append(block)
        elif not stripped:
            flush()
        elif m := re.match(r"(#{1,6})\s+(.*)", stripped):
            flush()
            content.append({"type": "heading", "attrs": {"level": min(len(m.group(1)), 6)},
                            "content": _text(m.group(2))})
        elif m := re.match(r"[-*+]\s+(?:\[([ xX])\]\s+)?(.*)", stripped):
            if para:
                content.append({"type": "paragraph", "content": _text(" ".join(para))})
                para.clear()
            box = {" ": "☐ ", "x": "☑ ", "X": "☑ "}.get(m.group(1) or "", "")
            bullets.append({"type": "listItem", "content": [{"type": "paragraph",
                                                              "content": _text(box + m.group(2))}]})
        else:
            if bullets:
                flush()
            para.append(stripped)
        i += 1
    flush()
    if not content:
        content = [{"type": "paragraph", "content": [{"type": "text", "text": " "}]}]
    return {"type": "doc", "version": 1, "content": content}


# --- The plan ---

reply = sys.stdin.buffer.read().decode("utf-8", errors="replace").strip()
try:
    markdown, plan = split(reply)
except ValueError as exc:
    print(f"The approved plan can't be read: {exc}", flush=True)
    sys.exit(1)

plans = Path(args.plans)
plans.mkdir(parents=True, exist_ok=True)
record_path = plans / f"crate-{args.crate}.json"
record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {}
record.update({"repo": f"jira:{project}", "service": "jira", "site": site, "label": label,
               "crate": int(args.crate), "title": str(plan.get("title") or "Plan").strip()[:100]})
tasks = record.setdefault("tasks", {})


def save() -> None:
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


issues = plan["issues"]
order: list[dict] = []
placed: set[str] = set()
while len(order) < len(issues):
    for issue in issues:
        if issue["key"] not in placed and set(issue["depends_on"]) <= placed:
            order.append(issue)
            placed.add(issue["key"])

title = record["title"]
for issue in order:
    key = issue["key"]
    if key in tasks:
        continue
    deps = [tasks[d]["number"] for d in issue["depends_on"]]
    body = str(issue.get("body", "")).strip()
    if len(issues) > 1:
        body += (f"\n\nTask {issues.index(issue) + 1} of {len(issues)} in the plan \"{title}\", made by the "
                 "Software Factory.")
        body += (" Waits for " + ", ".join(deps) + " to ship first." if deps else " Ready now: nothing it waits for.")
    ready = not deps
    fields = {"project": {"key": project}, "issuetype": {"name": issue_type},
              "summary": str(issue["title"]).strip()[:250], "description": adf(body)}
    if ready and label:
        fields["labels"] = [label]
    made = jira("POST", "/rest/api/3/issue", {"fields": fields})
    jkey = str(made.get("key", ""))
    if not jkey:
        print(f"Jira didn't say which issue it created for {key}: {made}", flush=True)
        sys.exit(1)
    url = f"{site}/browse/{jkey}"
    tasks[key] = {"number": jkey, "title": str(issue["title"]).strip(), "url": url,
                  "depends_on": issue["depends_on"], "labelled": ready, "shipped": False}
    save()
    for dep in deps:  # "dep blocks jkey": shows as "is blocked by" on the new issue
        req = urllib.request.Request(site + "/rest/api/3/issueLink", method="POST",
                                     data=json.dumps({"type": {"name": "Blocks"}, "inwardIssue": {"key": jkey},
                                                      "outwardIssue": {"key": dep}}).encode(),
                                     headers={"Authorization": f"Basic {AUTH}", "Accept": "application/json",
                                              "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30).read()
        except urllib.error.URLError as e:  # the issue's text still says what it waits for
            print(f"  (couldn't link {jkey} as blocked by {dep}: {e})", flush=True)
    print(f"{key}  {jkey}  {issue['title']}  {url}" + ("  (ready)" if ready else f"  (waits for {', '.join(deps)})"),
          flush=True)

ready_n = sum(1 for t in tasks.values() if t["labelled"])
if len(issues) == 1:
    t = tasks[issues[0]["key"]]
    print(f"Created the Jira issue {t['url']}" + (f", labelled {label}." if t["labelled"] and label else "."), flush=True)
else:
    print(f"Created {len(issues)} Jira issues in {project}, {ready_n} ready now"
          + (f" (labelled {label}); the rest get it as the ones they wait for ship." if label else "."), flush=True)
