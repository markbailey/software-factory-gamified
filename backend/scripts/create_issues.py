"""Issue maker: create the plan you approved as GitHub issues.

stdin: the Planner's reply (Markdown, then the ```json block with the issues).
Creates a tracking issue holding the plan, then one issue per task saying which issue it's
part of and which ones it waits for. Tasks that wait for nothing get the factory's label
straight away, so the build line picks them up; the backend labels the rest as the tasks they
depend on ship (it reads the record this writes under --plans).

Safe to run again: what it already created is recorded in <plans>/crate-<id>.json and not
created twice. When the crate came from a PRD issue, it comments there with the plan's link.
A plan of a single issue (e.g. one the Document writer wrote from a crate's description) is
just that issue, with no tracking issue.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from plan_format import split

ap = argparse.ArgumentParser()
ap.add_argument("--repo", required=True)
ap.add_argument("--label", required=True)
ap.add_argument("--plans", required=True)
ap.add_argument("--crate", required=True)
ap.add_argument("--source", default="")
ap.add_argument("--source-ref", default="")
args = ap.parse_args()

PLAN_LABEL = "plan"
gh = shutil.which("gh")
if not gh:
    print("gh is not installed or not on PATH", flush=True)
    sys.exit(1)


def run(*a: str, stdin: str | None = None) -> str:
    r = subprocess.run([gh, *a], input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(f"gh {' '.join(a[:2])} failed: {(r.stderr or r.stdout).strip()}", flush=True)
        sys.exit(1)
    return r.stdout.strip()


def number_of(url: str) -> int:
    m = re.search(r"/issues/(\d+)", url)
    if not m:
        print(f"Couldn't read the issue number from: {url}", flush=True)
        sys.exit(1)
    return int(m.group(1))


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
record.update({"repo": args.repo, "label": args.label, "crate": int(args.crate),
               **({"prd": args.source_ref} if args.source == "prd" and args.source_ref else {}),
               "title": str(plan.get("title") or "Plan").strip()[:100]})
tasks = record.setdefault("tasks", {})


def save() -> None:
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


for label, color in ((args.label, "0E8A16"), (PLAN_LABEL, "5319E7")):
    subprocess.run([gh, "label", "create", label, "--repo", args.repo, "--color", color, "--force"],
                   capture_output=True, text=True)

prd = f"#{args.source_ref}" if args.source == "prd" and args.source_ref else ""
if args.source == "notion" and args.source_ref:  # a PRD page in Notion: link the page
    prd = f"[the PRD in Notion](https://www.notion.so/{args.source_ref.replace('-', '')})"
# One issue needs no tracking issue to list it (a record from before keeps the one it has).
single = len(plan["issues"]) == 1 and not record.get("tracking")
if not single and not record.get("tracking"):
    body = (f"Planned by the Software Factory{' from ' + prd if prd else ''}. The tasks are listed "
            f"below once they're created.\n\n{markdown}\n")
    url = run("issue", "create", "--repo", args.repo, "--title", f"Plan: {record['title']}",
              "--label", PLAN_LABEL, "--body-file", "-", stdin=body)
    record["tracking"] = number_of(url)
    record["tracking_url"] = url
    save()
    print(f"Tracking issue: {url}", flush=True)
tracking = record.get("tracking")

# Create in dependency order, so the issues each one waits for already have numbers.
issues = plan["issues"]
order: list[dict] = []
placed: set[str] = set()
while len(order) < len(issues):
    for issue in issues:
        if issue["key"] not in placed and set(issue["depends_on"]) <= placed:
            order.append(issue)
            placed.add(issue["key"])

for issue in order:
    key = issue["key"]
    if key in tasks:
        continue
    deps = [tasks[d]["number"] for d in issue["depends_on"]]
    body = str(issue.get("body", "")).strip()
    if tracking:
        # Says where it sits in the plan, so a task isn't mistaken for the whole plan again.
        body += f"\n\nTask {issues.index(issue) + 1} of {len(issues)} in the plan #{tracking}."
        body += (" Waits for " + ", ".join(f"#{n}" for n in deps) + " to ship first." if deps
                 else " Ready now: nothing it waits for.")
    ready = not deps
    cmd = ["issue", "create", "--repo", args.repo, "--title", str(issue["title"]).strip()[:200],
           "--body-file", "-"]
    if ready:
        cmd += ["--label", args.label]
    url = run(*cmd, stdin=body + "\n")
    tasks[key] = {"number": number_of(url), "title": str(issue["title"]).strip(), "url": url,
                  "depends_on": issue["depends_on"], "labelled": ready, "shipped": False}
    save()
    print(f"{key}  #{tasks[key]['number']}  {issue['title']}" + ("  (ready)" if ready else
          f"  (waits for {', '.join('#' + str(n) for n in deps)})"), flush=True)

if single:
    t = tasks[plan["issues"][0]["key"]]
    if prd and args.source == "prd" and not record.get("commented"):
        run("issue", "comment", args.source_ref, "--repo", args.repo, "--body-file", "-",
            stdin=f"The Software Factory wrote this up as #{t['number']}"
                  + (f", labelled `{args.label}` for the build line.\n" if t["labelled"] else ".\n"))
        record["commented"] = True
        save()
    print(f"Created the issue {t['url']}" + (" (ready)." if t["labelled"] else "."), flush=True)
    sys.exit(0)

# The tracking issue lists the tasks; GitHub ticks each one as its issue closes.
lines = []
for issue in issues:
    t = tasks[issue["key"]]
    after = [f"#{tasks[d]['number']}" for d in issue["depends_on"]]
    lines.append(f"- [ ] #{t['number']}" + (f" (after {', '.join(after)})" if after else ""))
body = (f"Planned by the Software Factory{' from ' + prd if prd else ''}.\n\n## Tasks\n\n"
        + "\n".join(lines) + f"\n\n{markdown}\n")
run("issue", "edit", str(tracking), "--repo", args.repo, "--body-file", "-", stdin=body)

if prd and args.source == "prd" and not record.get("commented"):
    run("issue", "comment", args.source_ref, "--repo", args.repo, "--body-file", "-",
        stdin=f"The Software Factory planned this in #{tracking}: {len(issues)} issue"
              f"{'' if len(issues) == 1 else 's'}. Ready ones are labelled `{args.label}` and go to the "
              f"build line; the rest follow as the ones they wait for ship.\n")
    record["commented"] = True
    save()

ready = sum(1 for t in tasks.values() if t["labelled"])
print(f"Created the plan in {record['tracking_url']}: {len(issues)} issues, {ready} ready now.", flush=True)
