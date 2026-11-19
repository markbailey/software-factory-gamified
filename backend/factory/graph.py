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
the intake gives the order crates travel in, and must end at a dock or a scrap bin. Failed
crates leave by the fail belt, wherever it runs: back to an earlier machine on that path
(rework), into a scrap bin, or on to any other machine or dock (a branch, like a sorter's:
the crate carries on along that machine's belts, and its route records "<machine>=fail").
Only a machine whose fail belt runs to the bin scraps a crate there, and only for the
reasons in its "scrap_when" (all of them unless set). A crate it can't send on (no fail
belt, a reason it doesn't scrap for, or rework used up) waits at the machine until you run
it again or scrap it yourself. A crate whose belts end in a bin is scrapped there.
"path" is the belt's cells as the game laid them out; the backend only stores it.

"storages" are the places issues and PRs come in from: each holds one source ("github",
"jira", "pr_review", "prd" or "github_pr", open PRs to check are still needed) with its own
filter ("query": a JQL or a GitHub search), and has a belt from its "out" port into the
pipeline's intake. "sources" holds only "game" (specs typed in the game), when the intake
takes those. sources_of() gives both together. Each source feeds at most one pipeline,
except that one with its own filter can feed as many as have different filters, and GitHub
issues and PRDs come from the pipeline's own repo, so pipelines on different repos can each
take them. Files saved before storages (version < 5) list every source in "sources" (or a
single "source"), with the searches in "queries"; they're moved into storages on load.

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
SOURCES = ("game", "github", "jira", "pr_review", "prd", "github_pr", "custom")
# What a crate already has when it enters from this source (a PR to review comes with its PR).
SOURCE_PROVIDES = {"pr_review": ("pr",), "github_pr": ("pr",)}
# Sources that read the pipeline's own repo (its issues), so one per repo rather than one in all.
REPO_SOURCES = ("github", "prd", "github_pr")
SOURCE_WORDS = {"game": "typed specs", "github": "GitHub issues", "jira": "Jira issues",
                "pr_review": "PRs to review", "prd": "PRDs", "github_pr": "open PRs", "custom": "changes you describe"}


def repo_settings(p: dict) -> dict:
    """A pipeline's repo settings, cleaned: only the keys that are set."""
    raw = p.get("repo") if isinstance(p.get("repo"), dict) else {}
    out = {}
    for key in ("github", "path", "base_branch", "branch_pattern"):
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
    from .branch_names import validate as validate_pattern  # a line's own branch naming pattern
    error = validate_pattern(raw.get("branch_pattern", ""))
    return f"branch naming: {error}" if error else None


def missing_repo(p: dict) -> bool:
    """A line that works on its own repo but hasn't picked one. There's no default repo: it's
    paused until you pick one. A line whose crates all bring their repo with them (PRs to
    review, storages with a repo of their own) needs none."""
    if repo_key(p):
        return False
    if "storages" not in p:
        return any(s != "pr_review" for s in sources_of(p))
    if "game" in sources_of(p):
        return True
    for st in fed_storages(p):
        source = st.get("source")
        if source == "pr_review" or lists_repos(st) or (source in WORK_REPO_SOURCES and github_name(str(st.get("repo") or ""))):
            continue
        return True
    return False


def repo_key(p: dict) -> str:
    """Which repo a pipeline is on, for telling pipelines apart ("" is none picked)."""
    raw = repo_settings(p)
    return (github_name(raw.get("github", "")) or raw.get("path", "")).lower()
STATIONS = ("intake", "dock", "scrap")
RESERVED_PREFIXES = ("belt_", "return_", "rework_in_", "scrap_", "store_")
# Storages: "storages": [{"id": "store_1", "source": "jira", "query": "...", "pos": [x, y]}, ...]
# ("enabled": false on one switched off),
# each with one belt from its "out" port into its own pipeline's intake. A storage watches one
# source (with its own search) and its intake takes crates from it. Ids are unique in the factory.
STORE_ID = re.compile(r"^store_\d+$")
# "custom": changes you describe in the storage's window in the game (typed specs, from that storage).
STORE_SOURCES = ("github", "jira", "github_pr", "pr_review", "prd", "custom")
# Storages whose own repo ("repo") is where their crates are worked on: the GitHub ones read
# from it too; a Jira storage's crates are coded in it.
WORK_REPO_SOURCES = ("github", "prd", "github_pr", "jira", "custom")


def storages_of(p: dict) -> list[dict]:
    """A pipeline's storages (each a dict with id, source, query and pos)."""
    raw = p.get("storages")
    return [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []


def storage_repo(p: dict, source: str) -> str:
    """The GitHub repo (owner/name) the storage of this source belted into p's intake reads,
    when it has its own; "" means the line's repo."""
    for st in fed_storages(p):
        if st.get("source") == source and str(st.get("repo") or "").strip():
            return github_name(str(st["repo"])) or ""
    return ""


def source_keys(p: dict) -> list[tuple[str, str]]:
    """(source, key) for each source a pipeline takes, where the key says what one pipeline at
    a time can take: GitHub issues, PRDs and open PRs per repo, and per filter for a source with
    its own filter. A line with storages has one per storage (several storages of a source can
    feed it, each from its own repo or with its own filter)."""
    def key(source: str, repo: str, query: str) -> str:
        k = f"{source}@{repo.lower()}" if source in REPO_SOURCES else source
        return k + ("?" + " ".join(query.split()).lower() if query.strip() else "")
    if "storages" not in p:
        queries = queries_of(p)
        return [(s, key(s, repo_key(p), queries.get(s, ""))) for s in sources_of(p)]
    out = [("game", "game")] if "game" in sources_of(p) else []
    for st in fed_storages(p):
        source = str(st.get("source", ""))
        if source == "custom":  # what you type goes to the storage you type it into: never shared
            out.append((source, f"custom#{st.get('id', '')}"))
            continue
        repo = (github_name(str(st.get("repo") or "")) or repo_key(p)) if source in REPO_SOURCES else ""
        if lists_repos(st):
            repo = "*factory-repos*"
        out.append((source, key(source, repo, str(st.get("query") or "") if source in QUERY_SOURCES else "")))
    return out


def storage_on(st: dict) -> bool:
    """Whether a storage is switched on. One switched off ("enabled": false) stays on the floor,
    belted in, but isn't checked and brings nothing in, as if it weren't belted into the intake."""
    return st.get("enabled", True) is not False


def fed_storages(p: dict, with_off: bool = False) -> list[dict]:
    """The storages whose belt runs into this pipeline's intake (switched-off ones too, with_off)."""
    into = {str(b.get("from", "")) for b in p.get("belts", [])
            if isinstance(b, dict) and b.get("port") == "out" and str(b.get("to", "")) in ("intake", f"{p.get('id')}:intake")}
    return [s for s in storages_of(p) if str(s.get("id", "")) in into and (with_off or storage_on(s))]
# Extra scrap bins you place in build mode: "bins": [{"id": "scrap_2", "pos": [x, y]}, ...].
# Red belts can run into any of a pipeline's bins ("scrap" or one of these).
BIN_ID = re.compile(r"^scrap_\d+$")


def bins_of(p: dict) -> list[str]:
    """The ids of a pipeline's scrap bins: "scrap" and any extra ones."""
    return ["scrap"] + [str(b.get("id", "")) for b in p.get("bins", []) if isinstance(b, dict)]


# Extra shipping docks, the same way: "docks": [{"id": "dock_2", "pos": [x, y]}, ...]. A line's
# crates ship into whichever dock its last belt runs into.
DOCK_ID = re.compile(r"^dock_\d+$")


def docks_of(p: dict) -> list[str]:
    """The ids of a pipeline's shipping docks: "dock" and any extra ones."""
    return ["dock"] + [str(d.get("id", "")) for d in p.get("docks", []) if isinstance(d, dict)]


def is_dock_node(node: str) -> bool:
    place = node.rsplit(":", 1)[-1]
    return place == "dock" or bool(DOCK_ID.match(place))
ROW = 3  # the row the default layout puts the line on
STRIDE = 4  # cells per belt (2) + machine (2) in the default layout


QUERY_SOURCES = ("github", "jira", "github_pr", "pr_review")  # sources a storage can search with its own filter


def queries_of(p: dict) -> dict[str, str]:
    """The intake's own searches: {"jira": JQL, "github": a GitHub issue search, "github_pr": a
    GitHub PR search (default author:@me)}. Set, the line
    takes the issues that match instead of the factory-wide JQL or the `factory` label."""
    if "storages" in p:  # a storage's filter
        raw = {str(s.get("source", "")): s.get("query", "") for s in fed_storages(p)}
    else:
        raw = p.get("queries") if isinstance(p.get("queries"), dict) else {}
    return {k: " ".join(str(raw.get(k) or "").split())[:1000] for k in QUERY_SOURCES if str(raw.get(k) or "").strip()}


def listed_repos_only(p: dict, source: str = "pr_review") -> bool:
    """A storage's "Only repos in the factory's repo list" tick: the storage of this source
    belted into p's intake only brings in PRs from the repos added under the gear (Repos)."""
    return any(st.get("source") == source and bool(st.get("listed_repos_only")) for st in fed_storages(p))


def lists_repos(st: dict) -> bool:
    """An Open PRs storage with "Only repos in the factory's repo list" ticked: it reads every repo
    added under the gear (Repos) instead of its one repo, and needs no repo of its own."""
    return st.get("source") == "github_pr" and bool(st.get("listed_repos_only"))


def includes_factory_prs(st: dict) -> bool:
    """An Open PRs storage's "Include the factory's own PRs" tick. Off (the default), PRs the
    factory opened for crates still on a line are left out, so a crate's PR isn't taken twice."""
    return st.get("source") == "github_pr" and bool(st.get("include_factory_prs"))


def max_crates(p: dict) -> int:
    """At most this many crates on the line at once (0: no limit). More wait at the intake, and
    its storages bring nothing new in while it's full."""
    try:
        return max(0, int(p.get("max_crates") or 0))
    except (TypeError, ValueError):
        return 0


def account_of(p: dict, service: str) -> str:
    """The account a line's machines (and storages without their own pick) work as on this
    service ("github" or "jira"): an account id, "@machine", or "" for the factory's default."""
    raw = p.get("accounts") if isinstance(p.get("accounts"), dict) else {}
    return str(raw.get(service) or "").strip()[:40]


def check_only(p: dict) -> bool:
    """The intake's "only check these issues" tick: crates are checked and labelled (a Verifier,
    a Labeller), never worked on, so nothing is assigned or moved to In Progress."""
    return bool(p.get("check_only"))


def sources_of(p: dict) -> list[str]:
    """The sources a pipeline's intake takes crates from: specs typed in the game if it's
    ticked, and the source of each storage belted into it."""
    raw = p.get("sources")
    own = [str(s) for s in raw] if isinstance(raw, list) else [str(p.get("source", "game"))]
    if "storages" not in p:  # saved before storages: the intake's ticks say it all
        return own
    return [s for s in own if s == "game"] + [str(s.get("source", "")) for s in fed_storages(p)]


# Machine types that were merged into another: old saved layouts load as the new type, with
# these settings so they keep working the way they did.
RENAMED_TYPES = {
    "pr_checks": ("ci", {}),  # CI check and CI monitor are one CI machine now
    "pr_reviewer": ("reviewer", {"fail_on_changes": False, "comment_on_pr": False}),
    "pr_poster": ("doc_writer", {"doc_kind": "review"}),  # the Review poster and Issue maker are one
    "issue_maker": ("doc_writer", {"doc_kind": "issues"}),  # Document writer, set to what they made
}
REVIEW_LINE_OPTIONS = RENAMED_TYPES["pr_reviewer"][1]


FILE_VERSION = 6  # 2: scrapping needs a fail belt to the scrap bin; 3: the Approver waits, not the Reviewer;
# 4: a Document writer writes the PR's title and description, not the Packager; 5: issues and PRs
# come from storages belted into the intake, not from its ticks; 6: no default repo, so lines
# that named none (and so worked on factory.toml's test project) name it (engine.load_pipelines)


def saved_version(path: Path = PIPELINES_FILE) -> int:
    """The version pipelines.json was saved as (FILE_VERSION when it's missing or unreadable)."""
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("version", 1) or 1)
    except (OSError, ValueError, TypeError, AttributeError):
        return FILE_VERSION


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
    if int(data.get("version", 1) or 1) < 3 and isinstance(pipelines, list):
        add_approvers(pipelines)
    if int(data.get("version", 1) or 1) < 4 and isinstance(pipelines, list):
        add_pr_writers(pipelines)
    if int(data.get("version", 1) or 1) < 5 and isinstance(pipelines, list):
        add_storages(pipelines)
    return pipelines


def _taken_cells(pipelines: list[dict]) -> set:
    """Every cell a machine, station, bin or laid belt covers, in the whole factory."""
    taken = set()
    for q in pipelines:
        if not isinstance(q, dict):
            continue
        for place in [q.get(s) for s in STATIONS] + list(q.get("machines", [])) + list(q.get("bins", [])) \
                + list(q.get("docks", [])) + storages_of(q):
            if isinstance(place, dict):
                taken.update(_footprint(place.get("pos")))
        for b in q.get("belts", []):
            for cell in b.get("path", []) if isinstance(b, dict) else []:
                if isinstance(cell, (list, tuple)) and len(cell) == 2:
                    taken.add((int(cell[0]), int(cell[1])))
        for conv in conveyors_of(q):
            taken.update(tuple(c) for c in conv["cells"])
    return taken


def conveyors_of(p: dict) -> list[dict]:
    """Conveyors laid by hand that link nothing: [{id, cells: [[x, y], ...], loop?}]. Only drawn;
    no crate rides them. Cells that don't follow on from the one before are cut off."""
    out = []
    raw = p.get("conveyors") if isinstance(p, dict) else None
    for i, conv in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(conv, dict) or not isinstance(conv.get("cells"), list):
            continue
        cells = []
        for c in conv["cells"][:2000]:
            if not (isinstance(c, (list, tuple)) and len(c) == 2):
                break
            try:
                cell = [int(c[0]), int(c[1])]
            except (TypeError, ValueError):
                break
            if cells and abs(cell[0] - cells[-1][0]) + abs(cell[1] - cells[-1][1]) != 1:
                break
            cells.append(cell)
        if not cells:
            continue
        item = {"id": str(conv.get("id") or f"conv_{i + 1}")[:40], "cells": cells}
        if conv.get("loop") and len(cells) >= 4:
            item["loop"] = True
        if conv.get("join") and not item.get("loop"):
            item["join"] = True  # runs into another conveyor's side
        for flag in ("feeds", "onto_link"):  # runs into a link's start / side: laid after links
            if conv.get(flag) and not item.get("loop"):
                item[flag] = True
        out.append(item)
    return out


def add_storages(pipelines: list[dict]) -> None:
    """Saved when the intake's ticks picked its sources: a storage for each ticked source but
    typed specs (with the intake's search for it), left of the intake and belted into it."""
    taken = _taken_cells(pipelines)
    n = 1
    for p in pipelines:
        if not isinstance(p, dict):
            continue
        raw = p.get("sources")
        ticked = [str(s) for s in raw] if isinstance(raw, list) else [str(p.get("source", "game"))]
        queries = p.get("queries") if isinstance(p.get("queries"), dict) else {}
        stores: list[dict] = []
        intake = _pos((p.get("intake") or {}).get("pos")) if isinstance(p.get("intake"), dict) else ()
        for source in ticked:
            if source not in STORE_SOURCES:
                continue
            pos = _spot_left_of(intake, taken)
            taken.update(_footprint(pos))
            sid = f"store_{n}"
            n += 1
            store = {"id": sid, "source": source, "pos": list(pos)}
            if str(queries.get(source) or "").strip():
                store["query"] = str(queries[source]).strip()
            stores.append(store)
            p.setdefault("belts", []).append({"from": sid, "port": "out", "to": "intake"})
        p["storages"] = stores
        p["sources"] = [s for s in ticked if s == "game"]
        p.pop("queries", None)
        p.pop("source", None)


def _spot_left_of(near: tuple[int, ...], taken: set) -> tuple[int, int]:
    """A free 2x2 place left of near (an intake's top-left cell), above or below if that's taken."""
    x0, y0 = (near + (0, 0))[:2] if len(near) >= 2 else (0, 0)
    for dx in (-4, -7, -10, -13):
        for dy in (0, 3, -3, 6, -6):
            x, y = x0 + dx, y0 + dy
            room = {(x + i, y + j) for i in range(-1, 3) for j in range(-1, 3)}
            if not room & taken:
                return (x, y)
    return (x0 - 16, y0)


def add_pr_writers(pipelines: list[dict]) -> None:
    """Saved when the Packager wrote the PR's description itself: put a Document writer in front
    of each Packager (on the belts that ran into it), so PRs keep their filled-in template."""
    taken = _taken_cells(pipelines)
    ids = {str(m.get("id")) for q in pipelines if isinstance(q, dict) for m in q.get("machines", []) if isinstance(m, dict)}
    for p in pipelines:
        if not isinstance(p, dict):
            continue
        belts = p.setdefault("belts", [])
        for m in list(p.get("machines", [])):
            if not isinstance(m, dict) or m.get("type") != "packager":
                continue
            mid = str(m["id"])
            wid, n = f"{mid}_writer", 2
            while wid in ids:
                wid, n = f"{mid}_writer_{n}", n + 1
            ids.add(wid)
            pos = _free_spot(_pos(m.get("pos")), taken)
            taken.update(_footprint(pos))
            p["machines"].append({"id": wid, "type": "doc_writer", "name": "PR writer", "pos": list(pos),
                                 "options": {"doc_kind": "pr"}})
            for q in pipelines:  # belts from any line can run into it
                for b in q.get("belts", []) if isinstance(q, dict) else []:
                    if isinstance(b, dict) and b.get("to") == mid and b.get("port") != "fail":
                        b["to"] = wid
                        b.pop("path", None)
            belts.append({"from": wid, "port": "pass", "to": mid})


def add_approvers(pipelines: list[dict]) -> None:
    """Saved when the Reviewer waited for your approval itself: put an Approver after each
    Reviewer that did, with its auto-approve and GitHub review settings, on its green belt
    (the Approver's red belt goes where the Reviewer's does). The Reviewer then only reviews."""
    taken = _taken_cells(pipelines)
    ids = {str(m.get("id")) for q in pipelines if isinstance(q, dict) for m in q.get("machines", []) if isinstance(m, dict)}
    for p in pipelines:
        if not isinstance(p, dict):
            continue
        belts = p.setdefault("belts", [])
        for m in list(p.get("machines", [])):
            if not isinstance(m, dict) or m.get("type") != "reviewer":
                continue
            options = m.get("options") if isinstance(m.get("options"), dict) else {}
            github = options.pop("github_review", True)
            waited = m.pop("gate", None) is not False  # unset meant the Reviewer's default: wait
            auto = m.pop("auto_approve", None)
            if not waited:
                continue
            mid = str(m["id"])
            aid = f"{mid}_approver"
            n = 2
            while aid in ids:
                aid, n = f"{mid}_approver_{n}", n + 1
            ids.add(aid)
            pos = _free_spot(_pos(m.get("pos")), taken)
            taken.update(_footprint(pos))
            approver = {"id": aid, "type": "approver", "name": "Approver", "pos": list(pos),
                        "options": {"github_review": bool(github)}}
            if auto:
                approver["auto_approve"] = auto
            if m.get("scrap_when") is not None:
                approver["scrap_when"] = m["scrap_when"]
            p["machines"].append(approver)
            for b in belts:
                if isinstance(b, dict) and b.get("from") == mid and b.get("port") == "pass":
                    belts.append({"from": aid, "port": "pass", "to": b["to"]})
                    b["to"] = aid
                    b.pop("path", None)
                    break
            fail = next((b for b in belts if isinstance(b, dict) and b.get("from") == mid and b.get("port") == "fail"), None)
            if fail is not None:
                belts.append({"from": aid, "port": "fail", "to": fail["to"]})


def _free_spot(near: tuple[int, ...], taken: set) -> tuple[int, int]:
    """The nearest 2x2 place to near (a machine's top-left cell) that nothing covers, with a
    cell of room around it for belts."""
    x0, y0 = (near + (0, 0))[:2] if len(near) >= 2 else (0, 0)
    for r in range(3, 40):
        for dx, dy in [(0, r), (r, 0), (0, -r), (-r, 0), (r, r), (-r, r), (r, -r), (-r, -r)]:
            x, y = x0 + dx, y0 + dy
            if x < 0 or y < 0:
                continue
            room = {(x + i, y + j) for i in range(-1, 3) for j in range(-1, 3)}
            if not room & taken:
                return (x, y)
    return (x0, y0 + 40)


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
    # Reviewer here only drafts a review; you approve it at the Approver, and the Review poster
    # posts it.
    entries = [{"type": "ci", "id": "pr_checks"},
               {"type": "reviewer", "id": "pr_reviewer", "name": "PR reviewer", "options": dict(REVIEW_LINE_OPTIONS)},
               *([{"type": "approver", "id": "pr_approver", "name": "Approve review",
                   "options": {"github_review": False}}] if "approver" in catalog else []),
               {"type": "doc_writer", "id": "pr_poster", "options": {"doc_kind": "review"}}]
    return from_line(entries, "review", "Review line", "pr_review", row=ROW + 8)


def plan_line(catalog: dict, pipelines: list[dict]) -> dict | None:
    """The Planning line (PRDs into plans and issues), laid out under the other lines, if its
    machines are in the catalogue."""
    kinds = ("planner", "doc_writer")
    if not all(k in catalog for k in kinds):
        return None
    rows = [ROW]
    for p in pipelines:
        for place in [p.get(s, {}) for s in STATIONS] + list(p.get("machines", [])) + list(p.get("bins", [])) + list(p.get("docks", [])):
            pos = place.get("pos") if isinstance(place, dict) else None
            if isinstance(pos, (list, tuple)) and len(pos) == 2:
                rows.append(int(pos[1]))
    entries = [{"type": "planner"}, {"type": "doc_writer", "options": {"doc_kind": "issues"}}]
    return from_line(entries, "plan", "Planning line", "prd", row=max(rows) + 6)


def is_station(node: str) -> bool:
    """intake, dock or a scrap bin ("scrap", "scrap_2"), bare or as "<pipeline>:<station>"."""
    place = node.rsplit(":", 1)[-1]
    return place in STATIONS or bool(BIN_ID.match(place)) or bool(DOCK_ID.match(place))


def is_bin_node(node: str) -> bool:
    place = node.rsplit(":", 1)[-1]
    return place == "scrap" or bool(BIN_ID.match(place))


def qualify(pid: str, node: str) -> str:
    """A belt end as the whole factory names it: machine ids are already unique; a station
    without its pipeline ("dock") belongs to the belt's own pipeline ("build:dock")."""
    node = str(node or "")
    return f"{pid}:{node}" if is_station(node) and ":" not in node else node


def rehome(pipelines) -> list[str]:
    """Put each machine on the line that runs crates through it. A machine placed while
    another line was picked in the build panel belongs to that line, though only this line's
    belts reach it; it would pause its line ("isn't on the path from the intake to the dock").
    A machine that no way from its own intake reaches, but that exactly one other line's ways
    do, moves to that line with the belts that leave it. Returns the machines it moved."""
    if not isinstance(pipelines, list):
        return []
    ps = [p for p in pipelines if isinstance(p, dict) and isinstance(p.get("machines", []), list)
          and isinstance(p.get("belts", []), list)]
    reach: dict[str, set] = {}
    for p in ps:
        try:
            reach[str(p.get("id", ""))] = {mid for way in paths(p, ps) for mid in way["order"]}
        except (KeyError, TypeError, AttributeError, ValueError):
            return []  # not a layout we can follow: check() will say what's wrong
    moved = []
    for q in ps:
        qid = str(q.get("id", ""))
        for m in list(q.get("machines", [])):
            mid = m.get("id") if isinstance(m, dict) else None
            if not mid or mid in reach.get(qid, set()):
                continue
            homes = [p for p in ps if mid in reach.get(str(p.get("id", "")), set())]
            if len(homes) != 1 or homes[0] is q:
                continue
            to = homes[0]
            tid = str(to.get("id", ""))
            q["machines"].remove(m)
            to.setdefault("machines", []).append(m)
            keep = []
            for b in q.get("belts", []):
                if not isinstance(b, dict) or b.get("from") != mid:
                    keep.append(b)
                    continue
                end = qualify(qid, str(b.get("to", "")))
                if end.startswith(tid + ":"):
                    end = end[len(tid) + 1:]
                to.setdefault("belts", []).append({**b, "to": end})
            q["belts"] = keep
            moved.append(mid)
    return moved


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
RULE_KINDS = ("risk", "source", "repo", "result", "text", "files", "size", "otherwise")
RULE_NEEDS = {"risk": "risk", "files": "worktree", "size": "worktree"}
RULE_WORDS = {"risk": "risk", "files": "the files it changes", "size": "the size of its change"}


SPLITTER = "splitter"  # a sorter with no rules: crates go out of its outputs in turn


def is_sorter(m: dict | None) -> bool:
    return isinstance(m, dict) and m.get("type") in (SORTER, SPLITTER)


FAIL = "fail"  # in a route: the crate left this machine by its red belt, onward (not for rework)


def parse_route(text: str) -> dict[str, str]:
    """A crate's choices at sorters, and the red belts it took onward, as saved
    ("sort_1=out_2,tester=fail"): {machine: port}."""
    route = {}
    for part in str(text or "").split(","):
        sorter, _, port = part.partition("=")
        if sorter.strip() and (port.strip() in SORTER_PORTS or port.strip() == FAIL):
            route[sorter.strip()] = port.strip()
    return route


def route_text(route: dict[str, str]) -> str:
    return ",".join(f"{k}={v}" for k, v in route.items())


def _next(out: dict, m: dict, route: dict[str, str] | None) -> str | None:
    """Where a crate leaving machine m goes: its pass belt, a sorter's chosen output, or its red
    belt if it failed and the red belt leads on."""
    if (route or {}).get(str(m["id"])) == FAIL:
        return out.get((str(m["id"]), FAIL))
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
    """Where this pipeline's crates end up: the dock its belts end at ("build:dock"), or a scrap
    bin (they're scrapped there)."""
    pipelines = pipelines if pipelines is not None else [pipeline]
    out = all_outputs(pipelines)
    last = chain(pipeline, pipelines, route)
    at = (_next(out, last[-1], route) if last else out.get((f"{pipeline['id']}:intake", "out"))) or ""
    return at if is_dock_node(at) or is_bin_node(at) else f"{pipeline['id']}:dock"


def paths(pipeline: dict, pipelines: list[dict] | None = None, limit: int = 64) -> list[dict]:
    """Every way through the pipeline from its intake, branching at sorters and at red belts that
    lead on (not back for rework, not into a bin): [{"order": [machine ids], "end": the node it
    stops at ("build:dock", a bin, or "" if a belt is missing), "route": {machine: port},
    "stop": "" (reached a dock or bin), "none" (nothing on an output), "loop" or "lost" (a belt
    into something that isn't a machine, dock or bin)}]."""
    pipelines = pipelines if pipelines is not None else [pipeline]
    by_id = all_machines(pipelines)
    out = all_outputs(pipelines)
    found: list[dict] = []

    def walk(at: str | None, order: list[str], route: dict[str, str]) -> None:
        while len(found) < limit:
            if at is None:
                found.append({"order": order, "end": "", "route": route, "stop": "none"})
                return
            if is_dock_node(at) or is_bin_node(at):
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
            onward = out.get((at, FAIL))
            if onward is not None and not is_bin_node(onward) and onward not in order:
                walk(onward, order, {**route, at: FAIL})  # its red belt leads on: a branch
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
        for key in ("name", "gate", "prompt", "timeout_seconds", "auto_approve", "scrap_when", "options", "rules",
                    "retries", "model", "effort", "enabled"):
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
        if r["when"] == "result" and value.partition(":")[0].strip() not in ("pass", "fail"):
            return f"the rule on output {r['port'][-1]} needs passed or failed"
        if r["when"] == "size" and not value.strip().isdigit():
            return f"the rule on output {r['port'][-1]} needs a number of changed lines"
        if r["when"] in ("source", "repo", "text", "files") and not value.strip():
            return f"the rule on output {r['port'][-1]} needs something to match"
    return None


def switched_off(m: dict, base: MachineConfig) -> bool:
    """A machine you switched off in the game ("enabled": false, on a type that can be): it runs
    nothing, and crates go straight out of its green port."""
    return base.can_switch_off and m.get("enabled", True) is False


def coder_mode(m: dict, base: MachineConfig) -> str:
    options = m.get("options") if isinstance(m.get("options"), dict) else {}
    return str(options.get("coder_mode") or base.options.get("coder_mode") or "implement")


def _test_writer_problem(catalog: dict[str, MachineConfig], m: dict, name: str, out: dict,
                         everywhere: dict[str, dict], label) -> str | None:
    """A Coder in Test writer only mode hands its tests straight to a Tester: its green belt
    must run into one (switched on)."""
    if m.get("type") != "coder" or coder_mode(m, catalog["coder"]) != "test_only":
        return None
    nxt = everywhere.get(out.get((str(m.get("id", "")), "pass"), ""))
    if nxt is not None and nxt.get("type") == "tester":
        if switched_off(nxt, catalog["tester"]):
            return (f"{name} is set to Test writer only, but the {label(str(nxt['id']))} its belt runs into is "
                    "switched off: switch it on, or set the Coder to another mode")
        return None
    return (f"{name} is set to Test writer only, so its green belt must run straight into a Tester "
            "(nothing in between), to run the tests it writes")


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
        for st in storages_of(p):
            sid = str(st.get("id", ""))
            if not STORE_ID.match(sid) or sid in seen_machines:
                return f"{pname}: storage id {sid!r} is missing or used twice", {}
            seen_machines.add(sid)
            if st.get("source") not in STORE_SOURCES:
                return f"{pname}: a storage has an unknown source {st.get('source')!r}", {}
            if str(st.get("repo") or "").strip() and not github_name(str(st["repo"])):
                return f"{pname}: a storage's repo {st['repo']!r} isn't a GitHub repo; write it as owner/name", {}

    problems: dict[str, str] = {}
    seen_sources: dict[str, str] = {}
    for p in pipelines:
        pid = str(p["id"])
        pname = str(p.get("name") or pid)
        error = validate_repo(repo_settings(p))
        error = f"{pname}: {error}" if error else _validate_pipeline(catalog, p, pname, set(), pipelines)
        if error is None:
            mine: dict[str, str] = {}
            for source, key in source_keys(p):
                if source not in SOURCES:
                    error = f"{pname}: unknown intake source {source!r}"
                    break
                if key in mine:
                    error = (f"{pname}: two storages of {SOURCE_WORDS.get(source, source)} feed its intake with the "
                             "same repo and filter; remove one")
                    break
                if key in seen_sources:
                    where = " from the same repo" if source in REPO_SOURCES else ""
                    error = (f"{pname} and {seen_sources[key]} both take {SOURCE_WORDS.get(source, source)}"
                             f"{where}" + ("; give their storages different filters, or remove one"
                                           if "storages" in p else "; untick it on one of them"))
                    break
                mine[key] = pname
            if error is None:
                seen_sources.update(mine)  # only a pipeline that runs claims its sources
        if error is None and missing_repo(p):
            repoless = [SOURCE_WORDS.get(str(st.get("source")), str(st.get("source"))) for st in fed_storages(p)
                        if st.get("source") in WORK_REPO_SOURCES and not github_name(str(st.get("repo") or ""))
                        and not lists_repos(st)]
            if repoless and "game" not in sources_of(p):
                error = (f"{pname}: the {' and '.join(repoless)} storage{'s have' if len(repoless) > 1 else ' has'} "
                         "no repo: click it and pick its GitHub repo, or pick a repo for the whole line "
                         "(build mode: its intake's settings)")
            else:
                error = f"{pname}: pick a repo for this line (build mode: its intake's settings)"
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
            return f"{pname} needs {'an' if station == 'intake' else 'a'} {station} placed on the floor"
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
    docks = docks_of(p) if p.get("dock") is not None else [d for d in docks_of(p) if d != "dock"]
    if not isinstance(p.get("docks", []), list) or len(set(docks)) != len(docks):
        return f"{pname}: its extra shipping docks are muddled (an id is used twice)"
    for d in p.get("docks", []):
        if not isinstance(d, dict) or not DOCK_ID.match(str(d.get("id", ""))) or len(_pos(d.get("pos"))) != 2:
            return f"{pname}: a shipping dock is missing its id or place"
        for cell in _footprint(d["pos"]):
            if cell in cells:
                return f"{pname}: a shipping dock overlaps {cells[cell]}"
            cells[cell] = "a shipping dock"
    store_ids = set()
    for st in storages_of(p):
        if len(_pos(st.get("pos"))) != 2:
            return f"{pname}: a storage is missing its place"
        store_ids.add(str(st.get("id", "")))
        for cell in _footprint(st["pos"]):
            if cell in cells:
                return f"{pname}: a storage overlaps {cells[cell]}"
            cells[cell] = "a storage"

    # Belts: each output port drives at most one belt. A belt belongs to the pipeline its
    # source is in, and may run into another pipeline's machine, dock or bin.
    everywhere = all_machines(pipelines)
    stations = {qualify(str(q.get("id", "")), st) for q in pipelines for st in ("dock", "scrap")
                if isinstance(q.get(st), dict)}
    stations |= {qualify(str(q.get("id", "")), str(b.get("id", ""))) for q in pipelines
                 for b in q.get("bins", []) if isinstance(b, dict)}
    stations |= {qualify(str(q.get("id", "")), str(d.get("id", ""))) for q in pipelines
                 for d in q.get("docks", []) if isinstance(d, dict)}
    seen_ports: set[tuple[str, str]] = set()
    for b in p.get("belts", []):
        src, port, dst = str(b.get("from", "")), b.get("port"), qualify(pid, str(b.get("to", "")))
        if src in store_ids:
            if port != "out" or dst != f"{pid}:intake":
                return f"{pname}: a storage's belt must run into its own line's intake"
            if (src, port) in seen_ports:
                return f"{pname}: a storage has two belts out"
            seen_ports.add((src, port))
            continue
        valid_src = src == "intake" or src in names
        valid_dst = dst in everywhere or dst in stations
        if not valid_src or not valid_dst:
            return f"{pname}: a belt joins something that isn't there ({src} to {b.get('to')})"
        expected = ("out",) if src == "intake" else SORTER_PORTS if is_sorter(everywhere.get(src)) else ("pass", "fail")
        if port not in expected:
            return f"{pname}: a belt leaves {names.get(src, src)} from an unknown port {port!r}"
        if (src, port) in seen_ports:
            return f"{pname}: {names.get(src, 'the intake')} has two belts on its {port} output"
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
            return (f"{pname}: {label(last)} has nothing on its output; connect it to the next machine, "
                    "the shipping dock or a scrap bin")
    visited = {mid for way in routes for mid in way["order"]}
    missing = [names[mid] for mid in names if mid not in visited]
    if missing:
        return f"{pname}: {', '.join(missing)} isn't on the path from the intake to the dock"
    for mid in names:
        error = _test_writer_problem(catalog, everywhere[mid], names[mid], out, everywhere, label)
        if error:
            return f"{pname}: {error}"
    for way in routes:
        error = _check_path(catalog, p, pname, way["order"], everywhere, out, label, way["route"])
        if error:
            return error
    return None


def _check_path(catalog: dict[str, MachineConfig], p: dict, pname: str, order: list[str],
                everywhere: dict[str, dict], out: dict, label, route: dict[str, str] | None = None) -> str | None:
    """Machine rules along one way through: needs/provides and unique types. A crate arrives
    with what every one of the intake's sources gives it; a machine it left by its red belt
    gave it nothing."""
    sources = sources_of(p)
    if sources:
        # A sorter's rules may keep some sources off this way (an output "from Open PRs" only
        # gets open PRs): check it only with the sources that can come down it.
        sources = reaching_sources(sources, order, everywhere, out, route)
        if not sources:
            return None  # no crate from this intake can go this way
    provided = [set(SOURCE_PROVIDES.get(s, ())) for s in sources]
    have: set[str] = set.intersection(*provided) if provided else set()
    seen_types: set[str] = set()
    names = {mid: label(mid) for mid in order}
    for mid in order:
        m = everywhere[mid]
        base = catalog[m["type"]]
        if base.unique and m["type"] in seen_types:
            return f"{pname}: there can only be one {base.name}"
        seen_types.add(m["type"])
        if switched_off(m, base):
            continue  # runs nothing: needs nothing and gives the crate nothing
        missing = missing_needs(base.needs, have)
        if missing:
            # Some of the intake's sources may bring it (PRs to review come with their PR) and
            # others not: say which to take away.
            without = [SOURCE_WORDS.get(s, s) for s in sources
                       if missing_needs(base.needs, set(SOURCE_PROVIDES.get(s, ())) | have)]
            if provided and len(without) < len(sources):
                return (f"{pname}: {names[mid]} needs {', '.join(need_words(n) for n in missing)} before it, "
                        f"and {' and '.join(without)} don't come with that: "
                        + ("remove their storage from the intake" if "storages" in p else "untick them in the intake's settings"))
            # A machine before it that would give it that, but the crate left it by its red belt.
            for prev in order[:order.index(mid)]:
                prev_base = catalog[everywhere[prev]["type"]]
                if (route or {}).get(prev) == FAIL and any(alt in prev_base.provides for n in missing for alt in n.split("|")):
                    return (f"{pname}: the {names[prev]}'s red belt runs into the {names[mid]}, but a crate that "
                            f"fails at the {names[prev]} doesn't have what the {names[mid]} needs. Delete that red "
                            f"belt, or run it back to an earlier machine or into a bin")
            return f"{pname}: {names[mid]} needs {', '.join(need_words(n) for n in missing)} before it"
        if is_sorter(m):
            for r in m.get("rules", []) or []:
                need = RULE_NEEDS.get(r.get("when"))
                if need and (mid, r.get("port")) in out and need not in have:
                    return (f"{pname}: {names[mid]} sorts by {RULE_WORDS[r['when']]}, so it needs "
                            f"{need_words(need)} before it")
        gate = base.gate if m.get("gate") is None or base.gate_setting != "choice" else bool(m.get("gate"))
        if m.get("auto_approve") and base.can_auto_approve and gate and "risk" not in have:
            return (f"{pname}: {names[mid]} approves by risk, so it needs a Risk analyser before it "
                    f"(or set it to always ask you)")
        if (route or {}).get(mid) != FAIL:
            have.update(base.provides)
    return None


def _words(value) -> list[str]:
    return [w.strip() for w in re.split(r"[,\n]", str(value or "")) if w.strip()]


def _rule_verdict(rule: dict, source: str, arrived: tuple[str, str, str, str]) -> bool | None:
    """Whether a sorter rule takes every crate from this source (True), none of them (False),
    or it depends on the crate (None): what the layout alone can tell."""
    kind, value = rule.get("when"), rule.get("value", "")
    if kind == "otherwise":
        return True
    if kind == "source":
        return (source or "game") in _words(value)
    if kind == "result":  # same as the engine's "Arrived as" rule
        result, mid, name, mtype = arrived
        want, _, at = str(value or "").partition(":")
        if result != want.strip():
            return False
        names = [w.lower() for w in _words(at)]
        return not names or any(w in (mid.lower(), name.lower(), mtype.lower()) for w in names)
    return None  # risk, repo, text, files, size: depends on the crate


def reaching_sources(sources: list[str], order: list[str], everywhere: dict[str, dict], out: dict,
                     route: dict[str, str] | None) -> list[str]:
    """The intake's sources whose crates can take this way through: at each sorter on it, the
    rules in output order (as the engine tries them) must be able to send that source's crates
    out of the output the way takes."""
    route = route or {}
    left = list(sources)
    for i, mid in enumerate(order):
        m = everywhere[mid]
        port = route.get(mid)
        if m.get("type") != SORTER or port not in SORTER_PORTS:
            continue
        # How crates arrive: from the last machine that worked on them (not a sorter or merger).
        arrived = ("pass", "", "", "")
        for prev in reversed(order[:i]):
            pm = everywhere[prev]
            if is_sorter(pm) or pm.get("type") == "merger":
                continue
            arrived = ("fail" if route.get(prev) == FAIL else "pass", str(prev),
                       str(pm.get("name") or ""), str(pm.get("type") or ""))
            break
        rules = sorted((r for r in m.get("rules", []) or [] if isinstance(r, dict) and (mid, r.get("port")) in out),
                       key=lambda r: str(r.get("port")))
        keep = []
        for source in left:
            for rule in rules:
                verdict = _rule_verdict(rule, source, arrived)
                if verdict is False:
                    continue
                if rule.get("port") == port:
                    keep.append(source)  # it does (or may) go this way
                    break
                if verdict:
                    break  # it surely goes out of an earlier output
        left = keep
    return left


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
