"""Plans the Issue maker created (workspace/plans/crate-<id>.json, written by
scripts/create_issues.py): which issues wait for which, so the next ones get the factory's
label as the ones before them ship, and the plan's tracking issue closes once every task is
done (shipped, or scrapped and closed)."""
from __future__ import annotations

import json
from pathlib import Path


def _records(plans: Path):
    for path in sorted(plans.glob("crate-*.json")):
        try:
            yield path, json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue


def plan_of(plans: Path, repo: str, number: int | str) -> dict | None:
    """The plan record that has issue `number` as a task (or as its tracking issue), if any."""
    for _, record in _records(plans) if plans.is_dir() else ():
        if repo and record.get("repo") and str(record["repo"]).lower() != repo.lower():
            continue
        if record.get("tracking") == number or any(t.get("number") == number for t in record.get("tasks", {}).values()):
            return record
    return None


def shipped(plans: Path, repo: str, number: int | str) -> list:
    """Mark issue `number` shipped. Returns the issues now ready to label (all they wait for
    has shipped); call labelled() for each once it's labelled."""
    ready: list[int] = []
    for path, record in _records(plans):
        if record.get("repo") != repo:
            continue
        tasks = record.get("tasks", {})
        hit = [t for t in tasks.values() if t.get("number") == number]
        if not hit:
            continue
        for t in hit:
            t["shipped"] = True
            t.pop("scrapped", None)  # reopened and sent in again after a scrap, then shipped
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        for t in tasks.values():
            if t.get("labelled") or t.get("shipped"):
                continue
            if all(tasks.get(d, {}).get("shipped") for d in t.get("depends_on", [])):
                ready.append(t["number"])  # an int on GitHub, a key like "SF-12" in Jira
    return ready


def labelled(plans: Path, repo: str, number: int | str) -> None:
    for path, record in _records(plans):
        if record.get("repo") != repo:
            continue
        changed = False
        for t in record.get("tasks", {}).values():
            if t.get("number") == number and not t.get("labelled"):
                t["labelled"] = changed = True
        if changed:
            path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def _dependents(tasks: dict, key: str) -> list[str]:
    """Keys of the tasks that wait for `key`, directly or through another task."""
    out: list[str] = []
    todo = [key]
    while todo:
        k = todo.pop()
        for other, t in tasks.items():
            if k in t.get("depends_on", []) and other not in out:
                out.append(other)
                todo.append(other)
    return out


def scrapped(plans: Path, repo: str, number: int) -> list[dict]:
    """Mark issue `number` scrapped (closed as not planned). Returns one entry per plan it's in
    that has a tracking issue: {"tracking": n, "blocked": [issues still waiting for it]}."""
    out: list[dict] = []
    for path, record in _records(plans):
        if record.get("repo") != repo:
            continue
        tasks = record.get("tasks", {})
        keys = [k for k, t in tasks.items() if t.get("number") == number]
        if not keys:
            continue
        for k in keys:
            tasks[k]["scrapped"] = True
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        if not record.get("tracking"):
            continue
        blocked = sorted({int(tasks[d]["number"]) for k in keys for d in _dependents(tasks, k)
                          if not tasks[d].get("shipped") and not tasks[d].get("scrapped")})
        out.append({"tracking": int(record["tracking"]), "blocked": blocked})
    return out


def finished(plans: Path, repo: str, number: int) -> list[dict]:
    """The plans holding issue `number` whose tasks are now all done (shipped or scrapped) and
    whose tracking issue isn't closed yet: {"tracking", "shipped", "scrapped"} (issue numbers).
    Call closed() once its tracking issue is closed."""
    out: list[dict] = []
    for _, record in _records(plans):
        tasks = record.get("tasks", {}).values()
        if record.get("repo") != repo or not record.get("tracking") or record.get("closed"):
            continue
        if not any(t.get("number") == number for t in tasks):
            continue
        if all(t.get("shipped") or t.get("scrapped") for t in tasks):
            out.append({"tracking": int(record["tracking"]),
                        "shipped": [int(t["number"]) for t in tasks if t.get("shipped")],
                        "scrapped": [int(t["number"]) for t in tasks if not t.get("shipped")]})
    return out


def closed(plans: Path, repo: str, tracking: int) -> None:
    for path, record in _records(plans):
        if record.get("repo") == repo and record.get("tracking") == tracking and not record.get("closed"):
            record["closed"] = True
            path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
