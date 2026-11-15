"""Plans the Issue maker created (workspace/plans/crate-<id>.json, written by
scripts/create_issues.py): which issues wait for which, so the next ones get the factory's
label as the ones before them ship."""
from __future__ import annotations

import json
from pathlib import Path


def _records(plans: Path):
    for path in sorted(plans.glob("crate-*.json")):
        try:
            yield path, json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue


def shipped(plans: Path, repo: str, number: int) -> list[int]:
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
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        for t in tasks.values():
            if t.get("labelled") or t.get("shipped"):
                continue
            if all(tasks.get(d, {}).get("shipped") for d in t.get("depends_on", [])):
                ready.append(int(t["number"]))
    return ready


def labelled(plans: Path, repo: str, number: int) -> None:
    for path, record in _records(plans):
        if record.get("repo") != repo:
            continue
        changed = False
        for t in record.get("tasks", {}).values():
            if t.get("number") == number and not t.get("labelled"):
                t["labelled"] = changed = True
        if changed:
            path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
