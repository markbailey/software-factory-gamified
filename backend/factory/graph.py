"""Pipelines you build by hand in the game: machines placed on a grid and joined by belts.

A pipeline is a small graph saved in pipelines.json:

    {"pipelines": [{
        "id": "build", "name": "Build", "sources": ["game", "github", "jira"],
        "intake": {"pos": [1, 3]}, "dock": {"pos": [30, 3]}, "scrap": {"pos": [14, 7]},
        "machines": [{"id": "coder", "type": "coder", "name": "Coder", "pos": [5, 3],
                      "gate": false, "prompt": "", "timeout_seconds": null}, ...],
        "belts": [{"from": "intake", "port": "out", "to": "coder", "path": [[3, 3], [4, 3]]},
                  {"from": "coder", "port": "pass", "to": "tester"},
                  {"from": "tester", "port": "fail", "to": "coder"}, ...]
    }]}

Every machine has a "pass" output and an optional "fail" output. Following pass belts from
the intake gives the order crates travel in, and must end at the dock. Failed crates leave
by the fail belt: back to an earlier machine on that path (rework), or into the scrap bin.
Only a machine whose fail belt runs to the bin ever scraps a crate, and only for the reasons
in its "scrap_when" (all of them unless set). A crate it can't send on (no fail belt, a
reason it doesn't scrap for, or rework used up) waits at the machine until you run it again
or scrap it yourself.
"path" is the belt's cells as the game laid them out; the backend only stores it.

sources says where a pipeline's crates come from (its intake's settings): any of "game"
(typed in the game), "github", "jira", "pr_review" and "prd". Each source feeds at most one
pipeline, except that GitHub issues and PRDs come from the pipeline's own repo, so pipelines
on different repos can each take them. Older files have a single "source" instead;
sources_of() reads both.

"repo" is the repo the pipeline works on: {"github": "owner/name", "path": "folder",
"base_branch": "main"}, each optional (config.resolve_repo). Left out, it's the repo in
factory.toml's [workspace].
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import BACKEND_DIR, SCRAP_KINDS, MachineConfig, github_name, missing_needs, need_words

PIPELINES_FILE = BACKEND_DIR / "pipelines.json"
SOURCES = ("game", "github", "jira", "pr_review", "prd")
# What a crate already has when it enters from this source (a PR to review comes with its PR).
SOURCE_PROVIDES = {"pr_review": ("pr",)}
# Sources that read the pipeline's own repo (its issues), so one per repo rather than one in all.
REPO_SOURCES = ("github", "prd")
SOURCE_WORDS = {"game": "typed specs", "github": "GitHub issues", "jira": "Jira issues",
                "pr_review": "PRs to review", "prd": "PRDs"}


def repo_settings(p: dict) -> dict:
    """A pipeline's repo settings, cleaned: only the keys that are set."""
    raw = p.get("repo") if isinstance(p.get("repo"), dict) else {}
    out = {}
    for key in ("github", "path", "base_branch"):
        value = str(raw.get(key, "") or "").strip()
        if value:
            out[key] = value
    return out


def validate_repo(raw: dict) -> str | None:
    """What's wrong with a pipeline's repo settings, or None."""
    if raw.get("github") and not github_name(raw["github"]):
        return f"{raw['github']!r} isn't a GitHub repo; write it as owner/name"
    branch = raw.get("base_branch", "")
    if branch and (any(c in branch for c in " ~^:?*[\\") or branch.startswith("-") or ".." in branch):
        return f"{branch!r} isn't a branch name"
    return None


def repo_key(p: dict) -> str:
    """Which repo a pipeline is on, for telling pipelines apart ("" is the default repo)."""
    raw = repo_settings(p)
    return (github_name(raw.get("github", "")) or raw.get("path", "")).lower()
STATIONS = ("intake", "dock", "scrap")
RESERVED_PREFIXES = ("belt_", "return_", "rework_in_", "scrap_")
# Extra scrap bins you place in build mode: "bins": [{"id": "scrap_2", "pos": [x, y]}, ...].
# Red belts can run into any of a pipeline's bins ("scrap" or one of these).
BIN_ID = re.compile(r"^scrap_\d+$")


def bins_of(p: dict) -> list[str]:
    """The ids of a pipeline's scrap bins: "scrap" and any extra ones."""
    return ["scrap"] + [str(b.get("id", "")) for b in p.get("bins", []) if isinstance(b, dict)]
ROW = 3  # the row the default layout puts the line on
STRIDE = 4  # cells per belt (2) + machine (2) in the default layout


def sources_of(p: dict) -> list[str]:
    """The sources a pipeline's intake takes crates from."""
    raw = p.get("sources")
    if isinstance(raw, list):
        return [str(s) for s in raw]
    return [str(p.get("source", "game"))]


# Machine types that were merged into another: old saved layouts load as the new type, with
# these settings so they keep working the way they did.
RENAMED_TYPES = {
    "pr_checks": ("ci", {}),  # CI check and CI monitor are one CI machine now
    "pr_reviewer": ("reviewer", {"fail_on_changes": False, "comment_on_pr": False, "github_review": False}),
    "pr_poster": ("doc_writer", {}),  # the Review poster and Issue maker are one Document writer
    "issue_maker": ("doc_writer", {}),
}
REVIEW_LINE_OPTIONS = RENAMED_TYPES["pr_reviewer"][1]


FILE_VERSION = 2  # 2: scrapping needs a fail belt to the scrap bin


def read(path: Path = PIPELINES_FILE) -> list[dict] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    pipelines = migrate(data.get("pipelines"))
    if int(data.get("version", 1) or 1) < 2 and isinstance(pipelines, list):
        # Saved when a machine with no fail belt scrapped by itself: give each of those a fail
        # belt to its bin, so nothing changes until you remove the ones you don't want.
        for p in pipelines:
            if not isinstance(p, dict):
                continue
            belts = p.setdefault("belts", [])
            for m in p.get("machines", []):
                mid = m.get("id") if isinstance(m, dict) else None
                if mid and not any(b.get("from") == mid and b.get("port") == "fail" for b in belts):
                    belts.append({"from": mid, "port": "fail", "to": "scrap"})
    return pipelines


def migrate(pipelines):
    """Bring older pipelines up to date in place (renamed machine types). Returns them."""
    if isinstance(pipelines, list):
        for p in pipelines:
            for m in p.get("machines", []) if isinstance(p, dict) else []:
                if isinstance(m, dict) and m.get("type") in RENAMED_TYPES:
                    m["type"], options = RENAMED_TYPES[m["type"]]
                    if options:
                        m["options"] = {**options, **(m.get("options") or {})}
    return pipelines


def write(pipelines: list[dict], path: Path = PIPELINES_FILE) -> None:
    path.write_text(json.dumps({"version": FILE_VERSION, "pipelines": pipelines}, indent=2) + "\n",
                    encoding="utf-8")


def from_line(entries: list[dict], pipeline_id: str = "build", name: str = "Build",
              source: str = "game", row: int = ROW) -> dict:
    """Turn a straight line (line.json entries: type, id, name, gate, prompt, on_fail) into a
    pipeline, laid out in a row the way the game has always drawn it."""
    machines = []
    belts = []
    previous = "intake"
    col = 3
    for entry in entries:
        mid = str(entry.get("id") or entry["type"])
        machines.append({
            "id": mid, "type": entry["type"], "name": entry.get("name", ""),
            "pos": [col + 2, row], "gate": entry.get("gate"), "prompt": entry.get("prompt", ""),
            **({"options": entry["options"]} if entry.get("options") else {}),
        })
        belts.append({"from": previous, "port": "out" if previous == "intake" else "pass", "to": mid})
        if entry.get("on_fail"):
            belts.append({"from": mid, "port": "fail", "to": entry["on_fail"]})
        else:  # new lines start with every other machine's fail belt running to the bin
            belts.append({"from": mid, "port": "fail", "to": "scrap"})
        previous = mid
        col += STRIDE
    belts.append({"from": previous, "port": "out" if previous == "intake" else "pass", "to": "dock"})
    return {
        "id": pipeline_id, "name": name, "sources": [source],
        "intake": {"pos": [1, row]}, "dock": {"pos": [col + 2, row]},
        "scrap": {"pos": [max(3, (col + 3) // 2), row + 4]},
        "machines": machines, "belts": belts,
    }


def review_line(catalog: dict) -> dict | None:
    """The PR review pipeline, laid out under the build line, if its machines are in the catalogue."""
    kinds = ("ci", "reviewer", "doc_writer")
    if not all(k in catalog for k in kinds):
        return None
    # The build line has the "ci" and "reviewer" ids; ids are unique across lines. The
    # Reviewer here only drafts a review for you to approve: the Review poster posts it.
    entries = [{"type": "ci", "id": "pr_checks"},
               {"type": "reviewer", "id": "pr_reviewer", "name": "PR reviewer", "options": dict(REVIEW_LINE_OPTIONS)},
               {"type": "doc_writer", "id": "pr_poster"}]
    return from_line(entries, "review", "Review line", "pr_review", row=ROW + 8)


def plan_line(catalog: dict, pipelines: list[dict]) -> dict | None:
    """The Planning line (PRDs into plans and issues), laid out under the other lines, if its
    machines are in the catalogue."""
    kinds = ("planner", "doc_writer")
    if not all(k in catalog for k in kinds):
        return None
    rows = [ROW]
    for p in pipelines:
        for place in [p.get(s, {}) for s in STATIONS] + list(p.get("machines", [])) + list(p.get("bins", [])):
            pos = place.get("pos") if isinstance(place, dict) else None
            if isinstance(pos, (list, tuple)) and len(pos) == 2:
                rows.append(int(pos[1]))
    return from_line([{"type": k} for k in kinds], "plan", "Planning line", "prd", row=max(rows) + 6)


def is_station(node: str) -> bool:
    """intake, dock or a scrap bin ("scrap", "scrap_2"), bare or as "<pipeline>:<station>"."""
    place = node.rsplit(":", 1)[-1]
    return place in STATIONS or bool(BIN_ID.match(place))


def is_bin_node(node: str) -> bool:
    place = node.rsplit(":", 1)[-1]
    return place == "scrap" or bool(BIN_ID.match(place))


def qualify(pid: str, node: str) -> str:
    """A belt end as the whole factory names it: machine ids are already unique; a station
    without its pipeline ("dock") belongs to the belt's own pipeline ("build:dock")."""
    node = str(node or "")
    return f"{pid}:{node}" if is_station(node) and ":" not in node else node


def all_outputs(pipelines: list[dict]) -> dict[tuple[str, str], str]:
    """(from, port) -> to for every belt in the factory, with stations qualified. Belts can run
    from one pipeline into another's machines, dock or bin: that's how lines join."""
    out: dict[tuple[str, str], str] = {}
    for p in pipelines:
        pid = str(p.get("id", ""))
        for b in p.get("belts", []):
            if isinstance(b, dict):
                out[(qualify(pid, str(b.get("from", ""))), str(b.get("port", "")))] = qualify(pid, str(b.get("to", "")))
    return out


def all_machines(pipelines: list[dict]) -> dict[str, dict]:
    return {str(m.get("id")): m for p in pipelines for m in p.get("machines", []) if isinstance(m, dict)}


SORTER = "sorter"  # the machine type that sends each crate out of one of its outputs by rules
SORTER_PORTS = tuple(f"out_{i}" for i in range(1, 7))  # out_1/2 north, out_3/4 east, out_5/6 south
# A sorter rule: {"port": "out_1", "when": <kind>, "value": "..."}. The first rule (in port
# order) that matches a crate picks its output.
RULE_KINDS = ("risk", "source", "text", "files", "size", "otherwise")
RULE_NEEDS = {"risk": "risk", "files": "worktree", "size": "worktree"}
RULE_WORDS = {"risk": "risk", "files": "the files it changes", "size": "the size of its change"}


def is_sorter(m: dict | None) -> bool:
    return isinstance(m, dict) and m.get("type") == SORTER


def parse_route(text: str) -> dict[str, str]:
    """A crate's choices at sorters, as saved ("sort_1=out_2,sort_2=out_5"): {sorter: port}."""
    route = {}
    for part in str(text or "").split(","):
        sorter, _, port = part.partition("=")
        if sorter.strip() and port.strip() in SORTER_PORTS:
            route[sorter.strip()] = port.strip()
    return route


def route_text(route: dict[str, str]) -> str:
    return ",".join(f"{k}={v}" for k, v in route.items())


def _next(out: dict, m: dict, route: dict[str, str] | None) -> str | None:
    """Where a crate leaving machine m goes: its pass belt, or a sorter's chosen output."""
    if is_sorter(m):
        port = (route or {}).get(str(m["id"]))
        return out.get((str(m["id"]), port)) if port else None
    return out.get((str(m["id"]), "pass"))


def chain(pipeline: dict, pipelines: list[dict] | None = None, route: dict[str, str] | None = None) -> list[dict]:
    """The machines in the order this pipeline's crates visit them (following pass belts from
    its intake, into other pipelines' machines where belts join them). At a sorter it follows
    the output in route ({sorter id: port}); without one the chain ends at the sorter.
    Assumes it's valid."""
    pipelines = pipelines if pipelines is not None else [pipeline]
    by_id = all_machines(pipelines)
    out = all_outputs(pipelines)
    order = []
    at = out.get((f"{pipeline['id']}:intake", "out"))
    while at and at in by_id and by_id[at] not in order:
        order.append(by_id[at])
        at = _next(out, by_id[at], route)
    return order


def end_of(pipeline: dict, pipelines: list[dict] | None = None, route: dict[str, str] | None = None) -> str:
    """Where this pipeline's crates ship: the dock its pass belts end at ("build:dock")."""
    pipelines = pipelines if pipelines is not None else [pipeline]
    out = all_outputs(pipelines)
    last = chain(pipeline, pipelines, route)
    at = (_next(out, last[-1], route) if last else out.get((f"{pipeline['id']}:intake", "out"))) or ""
    return at if at.endswith(":dock") else f"{pipeline['id']}:dock"


def paths(pipeline: dict, pipelines: list[dict] | None = None, limit: int = 64) -> list[dict]:
    """Every way through the pipeline from its intake, branching at sorters: [{"order": [machine
    ids], "end": the node it stops at ("build:dock", or "" if a belt is missing), "route":
    {sorter: port}, "stop": "" (reached a dock), "none" (nothing on an output), "loop" or
    "lost" (a belt into something that isn't a machine or dock)}]."""
    pipelines = pipelines if pipelines is not None else [pipeline]
    by_id = all_machines(pipelines)
    out = all_outputs(pipelines)
    found: list[dict] = []

    def walk(at: str | None, order: list[str], route: dict[str, str]) -> None:
        while len(found) < limit:
            if at is None:
                found.append({"order": order, "end": "", "route": route, "stop": "none"})
                return
            if at.endswith(":dock"):
                found.append({"order": order, "end": at, "route": route, "stop": ""})
                return
            if at not in by_id:
                found.append({"order": order, "end": at, "route": route, "stop": "lost"})
                return
            if at in order:
                found.append({"order": order, "end": at, "route": route, "stop": "loop"})
                return
            order = order + [at]
            if is_sorter(by_id[at]):
                ports = [port for port in SORTER_PORTS if (at, port) in out]
                if not ports:
                    found.append({"order": order, "end": "", "route": route, "stop": "none"})
                for port in ports:
                    walk(out[(at, port)], order, {**route, at: port})
                return
            at = out.get((at, "pass"))

    walk(out.get((f"{pipeline['id']}:intake", "out")), [], {})
    return found


def to_line(pipeline: dict, pipelines: list[dict] | None = None, route: dict[str, str] | None = None) -> list[dict]:
    """The pipeline as line entries (in travel order, with on_fail), which is what the engine
    runs. scrap_to is the qualified bin ("build:scrap"). At a sorter it goes the way route
    says, or stops there. Assumes the pipeline is valid."""
    pipelines = pipelines if pipelines is not None else [pipeline]
    out = all_outputs(pipelines)
    entries = []
    for m in chain(pipeline, pipelines, route):
        fail_to = out.get((m["id"], "fail"), "")
        to_bin = is_bin_node(fail_to) if fail_to else False
        entry = {"type": m["type"], "id": m["id"], "on_fail": "" if to_bin else fail_to,
                 "scrap_bin": to_bin, "scrap_to": fail_to if to_bin else ""}
        for key in ("name", "gate", "prompt", "timeout_seconds", "auto_approve", "scrap_when", "options", "rules"):
            if m.get(key) not in (None, ""):
                entry[key] = m[key]
        if is_sorter(m):  # only the outputs that have a belt can be chosen
            entry["rules"] = [r for r in m.get("rules", []) if isinstance(r, dict) and (m["id"], r.get("port")) in out]
        entries.append(entry)
    return entries


def rules_problem(rules) -> str | None:
    """What's wrong with a sorter's rules, or None."""
    if rules is None:
        return None
    if not isinstance(rules, list):
        return "its rules are muddled"
    ports = set()
    for r in rules:
        if not isinstance(r, dict) or r.get("port") not in SORTER_PORTS or r.get("when") not in RULE_KINDS:
            return f"it has a rule it doesn't understand ({r!r})"
        if r["port"] in ports:
            return f"it has two rules for output {r['port'][-1]}"
        ports.add(r["port"])
        value = str(r.get("value", "") or "")
        if len(value) > 300:
            return f"the rule on output {r['port'][-1]} is too long"
        if r["when"] == "risk" and value.rstrip("+") not in ("low", "medium", "high"):
            return f"the rule on output {r['port'][-1]} needs a risk (low, medium or high)"
        if r["when"] == "size" and not value.strip().isdigit():
            return f"the rule on output {r['port'][-1]} needs a number of changed lines"
        if r["when"] in ("source", "text", "files") and not value.strip():
            return f"the rule on output {r['port'][-1]} needs something to match"
    return None


def validate(catalog: dict[str, MachineConfig], pipelines: list) -> str | None:
    """Return a message saying what is wrong, in words for the person building it, or None."""
    fatal, problems = check(catalog, pipelines)
    return fatal or next(iter(problems.values()), None)


def check(catalog: dict[str, MachineConfig], pipelines: list) -> tuple[str | None, dict[str, str]]:
    """(what stops the layout being saved at all, {pipeline id: why that pipeline can't run}).

    A layout with no pipelines is fine (the factory just has nothing to do). A pipeline that
    can't run (a machine missing what it needs, no belt to the dock...) is still saved: it's
    paused with its problem shown until you fix it. Only things that muddle the whole layout
    (ids used twice, unknown machine types) stop a save."""
    if not isinstance(pipelines, list):
        return "the pipelines aren't a list", {}
    seen_pipelines: set[str] = set()
    seen_machines: set[str] = set()
    for p in pipelines:
        if not isinstance(p, dict):
            return "a pipeline is not an object", {}
        pid = str(p.get("id", ""))
        pname = str(p.get("name") or pid or "a pipeline")
        if not pid or pid in seen_pipelines or ":" in pid:
            return f"pipeline id {pid!r} is missing or used twice", {}
        seen_pipelines.add(pid)
        if not isinstance(p.get("machines", []), list) or not isinstance(p.get("belts", []), list):
            return f"{pname}: its machines or belts are muddled", {}
        for m in p.get("machines", []):
            mid = str(m.get("id", "")) if isinstance(m, dict) else ""
            kind = m.get("type") if isinstance(m, dict) else None
            if kind not in catalog:
                return f"{pname}: unknown machine type {kind!r}", {}
            if not mid or mid in seen_machines or mid in STATIONS or mid.startswith(RESERVED_PREFIXES) \
                    or ":" in mid:
                return f"{pname}: machine id {mid!r} is missing, used twice or reserved", {}
            seen_machines.add(mid)

    problems: dict[str, str] = {}
    seen_sources: dict[str, str] = {}
    for p in pipelines:
        pid = str(p["id"])
        pname = str(p.get("name") or pid)
        error = validate_repo(repo_settings(p))
        error = f"{pname}: {error}" if error else _validate_pipeline(catalog, p, pname, set(), pipelines)
        if error is None:
            mine: dict[str, str] = {}
            for source in sources_of(p):
                if source not in SOURCES:
                    error = f"{pname}: unknown intake source {source!r}"
                    break
                key = f"{source}@{repo_key(p)}" if source in REPO_SOURCES else source
                if key in mine:
                    error = f"{pname}: its intake lists {source} twice"
                    break
                if key in seen_sources:
                    where = " from the same repo" if source in REPO_SOURCES else ""
                    error = (f"{pname} and {seen_sources[key]} both take {SOURCE_WORDS.get(source, source)}"
                             f"{where}; untick it on one of them")
                    break
                mine[key] = pname
            if error is None:
                seen_sources.update(mine)  # only a pipeline that runs claims its sources
        if error:
            problems[pid] = error
    return None, problems


def _validate_pipeline(catalog: dict[str, MachineConfig], p: dict, pname: str,
                       seen_machines: set[str], pipelines: list[dict] | None = None) -> str | None:
    pipelines = pipelines if pipelines is not None else [p]
    pid = str(p.get("id", ""))
    machines = p.get("machines", [])
    names: dict[str, str] = {}
    cells: dict[tuple[int, int], str] = {}
    for m in machines:
        mid = str(m.get("id", ""))
        kind = m.get("type")
        if kind not in catalog:
            return f"{pname}: unknown machine type {kind!r}"
        label = m.get("name") or catalog[kind].name
        if not mid or mid in seen_machines or mid in STATIONS or mid.startswith(RESERVED_PREFIXES) \
                or ":" in mid:
            return f"{pname}: machine id {mid!r} is missing, used twice or reserved"
        seen_machines.add(mid)
        names[mid] = label
        when = m.get("scrap_when")
        if when is not None and (not isinstance(when, list) or any(k not in SCRAP_KINDS for k in when)):
            return f"{pname}: {label} has an unknown scrap reason in {when!r}"
        for cell in _footprint(m.get("pos")):
            if cell in cells:
                return f"{pname}: {label} overlaps {cells[cell]}"
            cells[cell] = label
    for station in STATIONS:
        if station != "intake" and p.get(station) is None:
            continue  # a line can ship into, and scrap into, another line's dock and bin
        if not isinstance(p.get(station), dict) or len(_pos(p[station].get("pos"))) != 2:
            return f"{pname} needs a {station} placed on the floor"
        for cell in _footprint(p[station]["pos"]):
            if cell in cells:
                return f"{pname}: the {station} overlaps {cells[cell]}"
            cells[cell] = f"the {station}"
    bins = bins_of(p) if p.get("scrap") is not None else [b for b in bins_of(p) if b != "scrap"]
    if not isinstance(p.get("bins", []), list) or len(set(bins)) != len(bins):
        return f"{pname}: its extra scrap bins are muddled (an id is used twice)"
    for b in p.get("bins", []):
        if not isinstance(b, dict) or not BIN_ID.match(str(b.get("id", ""))) or len(_pos(b.get("pos"))) != 2:
            return f"{pname}: a scrap bin is missing its id or place"
        for cell in _footprint(b["pos"]):
            if cell in cells:
                return f"{pname}: a scrap bin overlaps {cells[cell]}"
            cells[cell] = "a scrap bin"

    # Belts: each output port drives at most one belt. A belt belongs to the pipeline its
    # source is in, and may run into another pipeline's machine, dock or bin.
    everywhere = all_machines(pipelines)
    stations = {qualify(str(q.get("id", "")), st) for q in pipelines for st in ("dock", "scrap")
                if isinstance(q.get(st), dict)}
    stations |= {qualify(str(q.get("id", "")), str(b.get("id", ""))) for q in pipelines
                 for b in q.get("bins", []) if isinstance(b, dict)}
    seen_ports: set[tuple[str, str]] = set()
    for b in p.get("belts", []):
        src, port, dst = str(b.get("from", "")), b.get("port"), qualify(pid, str(b.get("to", "")))
        valid_src = src == "intake" or src in names
        valid_dst = dst in everywhere or dst in stations
        if not valid_src or not valid_dst:
            return f"{pname}: a belt joins something that isn't there ({src} to {b.get('to')})"
        if is_bin_node(dst) and port != "fail":
            return f"{pname}: only a red (fail) belt can lead to the scrap bin"
        expected = ("out",) if src == "intake" else SORTER_PORTS if is_sorter(everywhere.get(src)) else ("pass", "fail")
        if port not in expected:
            return f"{pname}: a belt leaves {names.get(src, src)} from an unknown port {port!r}"
        if (src, port) in seen_ports:
            return f"{pname}: {names.get(src, 'the intake')} has two belts on its {port} output"
        if port == "fail" and dst.endswith(":dock"):
            return f"{pname}: failures from {names[src]} can't go to the dock"
        seen_ports.add((src, port))
    out = all_outputs(pipelines)

    def label(mid: str) -> str:
        m = everywhere.get(mid, {})
        return names.get(mid) or str(m.get("name") or catalog[m["type"]].name if m.get("type") in catalog else mid)

    for mid in names:
        m = everywhere[mid]
        problem = rules_problem(m.get("rules")) if is_sorter(m) else None
        if problem:
            return f"{pname}: {names[mid]}: {problem}"

    # Every way from the intake (sorters branch) must reach a dock, maybe through other
    # lines' machines, and between them go through every machine of this pipeline.
    routes = paths(p, pipelines)
    if not routes or (routes[0]["stop"] == "none" and not routes[0]["order"]):
        return f"{pname}: connect the intake to the first machine"
    for way in routes:
        stop, order = way["stop"], way["order"]
        if stop == "loop":
            return f"{pname}: the belts loop back to {label(way['end'])} without reaching the dock"
        if stop == "lost":
            return f"{pname}: a belt from {label(order[-1]) if order else 'the intake'} goes nowhere a crate can carry on from"
        if stop == "none":
            last = order[-1]
            if is_sorter(everywhere[last]):
                return f"{pname}: {label(last)} has no belts out; connect at least one of its outputs"
            return (f"{pname}: {label(last)} has nothing on its output; connect it to the next machine or "
                    "the shipping dock")
    visited = {mid for way in routes for mid in way["order"]}
    missing = [names[mid] for mid in names if mid not in visited]
    if missing:
        return f"{pname}: {', '.join(missing)} isn't on the path from the intake to the dock"
    for way in routes:
        error = _check_path(catalog, p, pname, way["order"], everywhere, out, label)
        if error:
            return error
    return None


def _check_path(catalog: dict[str, MachineConfig], p: dict, pname: str, order: list[str],
                everywhere: dict[str, dict], out: dict, label) -> str | None:
    """Machine rules along one way through: needs/provides, unique types, and rework belts go back.
    A crate arrives with what every one of the intake's sources gives it."""
    provided = [set(SOURCE_PROVIDES.get(s, ())) for s in sources_of(p)]
    have: set[str] = set.intersection(*provided) if provided else set()
    seen_types: set[str] = set()
    names = {mid: label(mid) for mid in order}
    for i, mid in enumerate(order):
        m = everywhere[mid]
        base = catalog[m["type"]]
        if base.unique and m["type"] in seen_types:
            return f"{pname}: there can only be one {base.name}"
        seen_types.add(m["type"])
        missing = missing_needs(base.needs, have)
        if missing:
            # Some of the intake's sources may bring it (PRs to review come with their PR) and
            # others not: say which to untick.
            without = [SOURCE_WORDS.get(s, s) for s in sources_of(p)
                       if missing_needs(base.needs, set(SOURCE_PROVIDES.get(s, ())) | have)]
            if provided and len(without) < len(sources_of(p)):
                return (f"{pname}: {names[mid]} needs {', '.join(need_words(n) for n in missing)} before it, "
                        f"and {' and '.join(without)} don't come with that: untick them in the intake's settings")
            return f"{pname}: {names[mid]} needs {', '.join(need_words(n) for n in missing)} before it"
        if is_sorter(m):
            for r in m.get("rules", []) or []:
                need = RULE_NEEDS.get(r.get("when"))
                if need and (mid, r.get("port")) in out and need not in have:
                    return (f"{pname}: {names[mid]} sorts by {RULE_WORDS[r['when']]}, so it needs "
                            f"{need_words(need)} before it")
        gate = base.gate if m.get("gate") is None else bool(m.get("gate"))
        if m.get("auto_approve") and base.can_auto_approve and gate and "risk" not in have:
            return (f"{pname}: {names[mid]} approves by risk, so it needs a Risk analyser before it "
                    f"(or set it to always ask you)")
        have.update(base.provides)
        target = out.get((mid, "fail"))
        if target is not None and not is_bin_node(target) and target not in order[:i]:
            return f"{pname}: failures from {names[mid]} can only go back to a machine before it or to the scrap bin"
    return None


def _outputs(pipeline: dict) -> dict[tuple[str, str], str]:
    return {(b["from"], b["port"]): b["to"] for b in pipeline.get("belts", [])}


def _pos(raw) -> tuple[int, ...]:
    try:
        return tuple(int(v) for v in raw)
    except (TypeError, ValueError):
        return ()


def _footprint(raw) -> list[tuple[int, int]]:
    """The four cells a 2x2 machine or station covers, from its top-left cell."""
    pos = _pos(raw)
    if len(pos) != 2:
        return []
    x, y = pos
    return [(x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1)]
