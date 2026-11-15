"""Planner: check Claude's plan before it waits for you.

stdin: Claude's reply. Prints the plan's issues (so you can read them in the crate's log) and
exits 0. Exits 1 with what's wrong when the ```json block is missing or broken, so the crate
goes back to the Planner to fix it. When Claude asked questions instead ("Outcome: needs
info"), prints OUTCOME needs_info and NO_REWORK, so the PRD is handed back with them.
"""
import re
import sys

from plan_format import split

reply = sys.stdin.buffer.read().decode("utf-8", errors="replace").strip()
if re.search(r"^\W*outcome\W*\s*needs[ _-]?info\b", reply, re.IGNORECASE | re.MULTILINE):
    print("OUTCOME needs_info", flush=True)
    print("NO_REWORK", flush=True)
    print("Claude made no changes.", flush=True)
    print("Its explanation:", flush=True)
    print(reply, flush=True)
    sys.exit(1)
try:
    _, plan = split(reply)
except ValueError as exc:
    print(f"The plan isn't in the right shape: {exc} End the reply with the ```json block "
          "exactly as the instructions show.", flush=True)
    sys.exit(1)
issues = plan["issues"]
print(f"Plan: {plan.get('title') or 'untitled'} ({len(issues)} issue{'' if len(issues) == 1 else 's'})", flush=True)
for issue in issues:
    after = f"  (after {', '.join(issue['depends_on'])})" if issue["depends_on"] else ""
    print(f"  {issue['key']}  {issue['title']}{after}", flush=True)
