"""Factory stats for the game's Stats window: throughput, time per crate, failures, rework and
what Claude cost, per line and per machine, over the last few days."""
from __future__ import annotations

import statistics
import time
from datetime import datetime, timedelta

from .store import Store

DAY = 86400.0


def _days_back(days: int) -> list[float]:
    """Local midnights, oldest first, for the last `days` days (today included)."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return [(today - timedelta(days=k)).timestamp() for k in range(days - 1, -1, -1)]


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 3) if whole else 0.0


def compute(store: Store, names: dict[str, dict], days: int = 7) -> dict:
    """names: machine id -> {"name", "pipeline", "pipeline_name"} from the current layout, and
    line id -> {"name"} (machines no longer placed keep their id as name)."""
    days = max(1, min(int(days), 90))
    starts = _days_back(days)
    since = starts[0]
    done = store.finished_since(since)
    runs = store.runs_since(since)
    shipped = [c for c in done if c["state"] == "shipped"]
    scrapped = [c for c in done if c["state"] == "scrapped"]
    times = [c["finished_at"] - c["created_at"] for c in shipped if c["finished_at"] > c["created_at"]]

    per_day = []
    for k, start in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else start + DAY
        day_runs = [r for r in runs if start <= r["ended_at"] < end]
        per_day.append({
            "date": datetime.fromtimestamp(start).strftime("%a %d %b"),
            "shipped": sum(1 for c in shipped if start <= c["finished_at"] < end),
            "scrapped": sum(1 for c in scrapped if start <= c["finished_at"] < end),
            "cost_usd": round(sum(r["cost_usd"] for r in day_runs), 4),
        })

    machines: dict[str, dict] = {}
    for r in runs:
        m = machines.setdefault(r["machine"], {
            "id": r["machine"], "type": r["machine_type"], "pipeline": r["pipeline"],
            "name": names.get(r["machine"], {}).get("name", r["machine"]),
            "line": names.get(r["machine"], {}).get("pipeline_name", r["pipeline"]),
            "runs": 0, "failed": 0, "seconds": 0.0, "cost_usd": 0.0, "turns": 0, "failed_steps": {}})
        if r["outcome"] == "stopped":
            m["cost_usd"] += r["cost_usd"]
            continue
        m["runs"] += 1
        m["seconds"] += r["ended_at"] - r["started_at"]
        m["cost_usd"] += r["cost_usd"]
        m["turns"] += r["turns"]
        if r["outcome"] == "failed":
            m["failed"] += 1
            step = r["failed_step"] or "?"
            m["failed_steps"][step] = m["failed_steps"].get(step, 0) + 1
    machine_rows = []
    for m in machines.values():
        top = sorted(m.pop("failed_steps").items(), key=lambda kv: -kv[1])[:3]
        machine_rows.append({**m, "cost_usd": round(m["cost_usd"], 4),
                             "avg_seconds": round(m["seconds"] / m["runs"], 1) if m["runs"] else 0.0,
                             "fail_rate": _rate(m["failed"], m["runs"]),
                             "top_failures": [{"step": s, "count": n} for s, n in top]})
    machine_rows.sort(key=lambda m: (m["line"], m["name"]))

    lines: dict[str, dict] = {}
    for c in done:
        ln = lines.setdefault(c["pipeline"], {"id": c["pipeline"], "shipped": 0, "scrapped": 0, "cost_usd": 0.0,
                                              "name": names.get(c["pipeline"], {}).get("name", c["pipeline"])})
        ln[c["state"]] += 1
    for r in runs:
        ln = lines.setdefault(r["pipeline"], {"id": r["pipeline"], "shipped": 0, "scrapped": 0, "cost_usd": 0.0,
                                              "name": names.get(r["pipeline"], {}).get("name", r["pipeline"])})
        ln["cost_usd"] += r["cost_usd"]
    for ln in lines.values():
        ln["cost_usd"] = round(ln["cost_usd"], 4)
        ln["fail_rate"] = _rate(ln["scrapped"], ln["shipped"] + ln["scrapped"])

    total_cost = sum(r["cost_usd"] for r in runs)
    return {
        "type": "stats",
        "days": days,
        "at": time.time(),
        "shipped": len(shipped),
        "scrapped": len(scrapped),
        "per_day": per_day,
        "crate_minutes": {
            "median": round(statistics.median(times) / 60, 1) if times else 0.0,
            "mean": round(statistics.mean(times) / 60, 1) if times else 0.0,
            "longest": round(max(times) / 60, 1) if times else 0.0,
        },
        "fail_rate": _rate(len(scrapped), len(done)),
        "rework_rate": _rate(sum(1 for c in shipped if c["loops"]), len(shipped)),
        "scrap_kinds": {k: sum(1 for c in scrapped if (c["scrap_kind"] or "other") == k)
                        for k in sorted({c["scrap_kind"] or "other" for c in scrapped})},
        "cost_usd": round(total_cost, 4),
        "cost_per_shipped": round(total_cost / len(shipped), 4) if shipped else 0.0,
        "lines": sorted(lines.values(), key=lambda ln: ln["name"]),
        "machines": machine_rows,
    }
