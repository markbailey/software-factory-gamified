"""The Planner's reply: Markdown, then a fenced ```json block with the issues.
Shared by check_plan.py and create_issues.py."""
import json
import re

FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def split(reply: str) -> tuple[str, dict]:
    """(the Markdown before the JSON block, the plan). Raises ValueError saying what's wrong."""
    blocks = list(FENCE_RE.finditer(reply))
    if not blocks:
        raise ValueError("There is no ```json block with the issues at the end of the plan.")
    last = blocks[-1]
    try:
        plan = json.loads(last.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"The ```json block isn't valid JSON: {exc}") from None
    issues = plan.get("issues") if isinstance(plan, dict) else None
    if not isinstance(issues, list) or not issues:
        raise ValueError('The JSON needs an "issues" list with at least one issue.')
    keys = set()
    for n, issue in enumerate(issues, 1):
        if not isinstance(issue, dict) or not str(issue.get("title", "")).strip():
            raise ValueError(f"Issue {n} has no title.")
        issue["key"] = str(issue.get("key") or f"T{n}")
        if issue["key"] in keys:
            raise ValueError(f"Two issues have the key {issue['key']}.")
        keys.add(issue["key"])
        issue["depends_on"] = [str(d) for d in issue.get("depends_on") or []]
    for issue in issues:
        missing = [d for d in issue["depends_on"] if d not in keys]
        if missing:
            raise ValueError(f"{issue['key']} depends on {', '.join(missing)}, which isn't in the plan.")
    # No cycles: every issue must become ready once the ones before it are done.
    done: set[str] = set()
    while len(done) < len(issues):
        ready = [i["key"] for i in issues if i["key"] not in done and set(i["depends_on"]) <= done]
        if not ready:
            raise ValueError("The dependencies go round in a circle, so nothing could ever start.")
        done.update(ready)
    markdown = reply[:last.start()].rstrip()
    return markdown, plan
