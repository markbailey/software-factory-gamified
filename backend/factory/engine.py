"""The production line: intake -> belt_0 -> machine 1 -> belt_1 -> machine 2 -> ... -> dock.

A crate that fails at a machine whose red belt runs back to an earlier machine (on_fail)
goes back along it to that machine (the Coder) with the failure as its notes, keeping its
branch and PR, up to max_rework times. A red belt that leads on (to a later machine, another
line or a dock) takes the failed crate there, and it carries on along that machine's belts.
A red belt into a bin scraps it, but only for the reasons the machine scraps for
(scrap_when); otherwise the crate is held at the machine ("held") until you run it again or
scrap it. A crate whose belts end in a bin is scrapped there. Crates only move when this
code moves them, and every move is an event the game renders.
"""
from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from dataclasses import dataclass

from . import attribution, config, formats, graph, janitor, runner, stats, triage, workspace
from .config import Config, MachineConfig, RepoConfig, StepConfig, is_agent_step
from .health import AgentHealth
from .hub import Hub
from .store import Store

PROGRESS_RE = re.compile(r"^PROGRESS\s+([0-9]*\.?[0-9]+)\s*$")
PR_URL_RE = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+")
PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
# Printed by a Risk analyser step (scripts/stamp_risk.py): the rating stamped on the crate.
RISK_RE = re.compile(r"^RISK\s+(low|medium|high)\b\s*(.*)$", re.I)
# "WAITING <why>" from a step that is waiting on something outside (an approval on GitHub);
# a bare "WAITING" clears it. Shown on the crate and its machine; never a failure.
WAITING_RE = re.compile(r"^WAITING(?:\s+(.*))?$")
RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
REVIEW_POLL_SECONDS = 20
TOOL_CHECK_SECONDS = 30  # between checks of a broken tool
FAILURE_TAIL_LINES = 60
FAILURE_TAIL_CHARS = 6000

# Called when a crate reaches a milestone: (item, event, detail).
# event is "picked" (a crate started on it, or was retried), "pr_opened", "awaiting_approval",
# "auto_approved", "scrapped" (the item's scrap_kind says why) or "shipped".
Notifier = Callable[[dict, str, str], Awaitable[None]]


@dataclass
class Outage:
    """A broken tool (see triage.py): crates that need it wait until it's fixed."""
    problem: triage.ToolProblem
    detail: str
    since: float
    fixed: asyncio.Event


@dataclass
class Line:
    """One pipeline as the engine runs it: its machines in travel order."""
    id: str
    name: str
    sources: list[str]
    machines: list[MachineConfig]
    graph: dict
    repo: RepoConfig  # the repo its crates work on (config.resolve_repo)
    end: str = ""  # the dock its crates ship into ("build:dock"; maybe another line's)
    bin: str = ""  # the scrap bin crates go to when you scrap them yourself
    # The way through its sorters this view follows ("sort_1=out_2"; see graph.parse_route).
    # machines stop at the first sorter without a choice. Places on a route are named
    # "<pipeline>@<route>:belt_<i>", so the game can tell the branches apart.
    route: str = ""
    everyone: dict[str, MachineConfig] | None = None  # every machine on any of its ways through

    def at(self, place: str) -> str:
        """A location in this pipeline: intake, belt_<i> (in front of machine i; belt_<n> leads
        to the dock), return_<i> (the rework belt at machine i), dock or scrap. Machines are
        located by their own ids, which are unique across pipelines. A place that already names
        its pipeline ("plan:scrap": another line's bin) is left as it is."""
        if place == "dock" and self.end:
            return self.end
        if place == "scrap" and self.bin:
            return self.bin
        if ":" in place:
            return place
        return f"{self.id}@{self.route}:{place}" if self.route and place != "intake" else f"{self.id}:{place}"

    def belt(self, i: int) -> str:
        return self.at(f"belt_{i}")

    def return_belt(self, i: int) -> str:
        return self.at(f"return_{i}")


PAUSES_FILE = config.BACKEND_DIR / "paused.json"
REPO_PREP_SECONDS = 120.0  # a save gives up waiting for a repo's clone or fetch after this


def _repo_key(repo: RepoConfig) -> tuple[str, str]:
    return (str(repo.path).lower(), str(repo.github or "").lower())


def _read_pauses() -> dict:
    out = {"factory": False, "lines": [], "machines": []}
    try:
        data = json.loads(PAUSES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if isinstance(data, dict):
        out["factory"] = bool(data.get("factory", False))
        for key in ("lines", "machines"):
            if isinstance(data.get(key), list):
                out[key] = [str(i) for i in data[key]]
    return out


BELTS_FILE = config.BACKEND_DIR / "belts.json"
DEFAULT_BELT_SPEED = 1.5  # cells a second
BELT_SPEEDS = (0.5, 6.0)  # slowest and fastest you can set (faster strobes at 60 fps)


def _read_belt_speed() -> float:
    try:
        speed = float(json.loads(BELTS_FILE.read_text(encoding="utf-8")).get("speed", DEFAULT_BELT_SPEED))
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_BELT_SPEED
    return min(BELT_SPEEDS[1], max(BELT_SPEEDS[0], speed))


def _write_pauses(pauses: dict) -> None:
    try:
        PAUSES_FILE.write_text(json.dumps(pauses, indent=1), encoding="utf-8")
    except OSError as exc:
        print(f"[factory] could not save what's paused: {exc}")


def _resume_point(line: Line, location: str) -> tuple[int, str]:
    """Where a crate found at `location` after a restart carries on: (index of the machine to
    run next, words for where it was). A crate in a machine runs it again; one on the belt in
    front of machine i goes into it; one on a rework belt runs the machine that sent it back."""
    index = {m.id: i for i, m in enumerate(line.machines)}
    if location in index:
        return index[location], f"in the {line.machines[index[location]].name}"
    place = location.rsplit(":", 1)[-1]
    match = re.fullmatch(r"(belt|return)_(\d+)", place)
    if match:
        i = min(int(match.group(2)), len(line.machines))
        return i, ("on the belt to the dock" if i == len(line.machines) else
                   f"on the belt to the {line.machines[i].name}")
    return 0, "at the intake"


def build_lines(cfg: Config, pipelines: list[dict], everything: list[dict] | None = None) -> dict[str, Line]:
    """Lines for these pipelines. everything: the whole layout, which belts may run into (a
    line that joins another carries on along that line's machines to its dock)."""
    everything = everything if everything is not None else pipelines
    lines = {}
    for p in pipelines:
        line = line_view(cfg, p, everything)
        line.everyone = {}
        for way in graph.paths(p, everything):
            for m in line_view(cfg, p, everything, way["route"]).machines:
                line.everyone.setdefault(m.id, m)
        lines[p["id"]] = line
    return lines


def line_view(cfg: Config, p: dict, everything: list[dict], route: dict[str, str] | None = None) -> Line:
    """Pipeline p as the engine runs it along this way through its sorters."""
    bins = [f"{q['id']}:scrap" for q in everything if isinstance(q.get("scrap"), dict)]
    own_bin = f"{p['id']}:scrap" if isinstance(p.get("scrap"), dict) else (bins[0] if bins else f"{p['id']}:scrap")
    machines = config.build_line(cfg.catalog, graph.to_line(p, everything, route))
    for m in machines:
        if not m.scrap_to or m.scrap_to == "scrap":
            m.scrap_to = own_bin
    return Line(p["id"], p.get("name") or p["id"], graph.sources_of(p), machines, p,
                config.resolve_repo(cfg, p.get("repo")), end=graph.end_of(p, everything, route),
                bin=own_bin, route=graph.route_text(route or {}))


def normalize_repo(cfg: Config, p: dict) -> dict:
    """A pipeline's repo settings as saved: only what's set, and nothing at all for the
    default repo on its usual base branch (so pipelines on it compare as the same repo)."""
    raw = graph.repo_settings(p)
    if raw.get("github"):
        raw["github"] = config.github_name(raw["github"]) or raw["github"]
    if raw and not graph.validate_repo(raw):
        repo = config.resolve_repo(cfg, raw)
        if repo.is_default:
            raw = {"base_branch": raw["base_branch"]} if raw.get("base_branch") not in (
                None, "", cfg.default_repo.base_branch) else {}
    if raw:
        p["repo"] = raw
    else:
        p.pop("repo", None)
    return p


def load_pipelines(cfg: Config) -> list[dict]:
    """pipelines.json if it is usable (pipelines in it that can't run are kept, paused);
    otherwise the saved or default line as one pipeline, with the review and planning lines."""
    saved = graph.read()
    if saved is not None:
        moved = graph.rehome(saved)
        if moved:
            print(f"[factory] moved {', '.join(moved)} to the line that runs crates through them")
        error, _ = graph.check(cfg.catalog, saved)
        if not error:
            return saved
        print(f"[factory] pipelines.json is not usable ({error}); using the line instead")
    entries = [{"type": m.type, "id": m.id, "name": m.name, "gate": m.gate, "prompt": m.prompt
                if m.has_prompt() else "", "on_fail": m.on_fail} for m in cfg.machines]
    pipelines = [graph.from_line(entries)]
    pipelines[0]["sources"] = ["game", "github", "jira"]
    if "doc_writer" in cfg.catalog:
        graph.add_pr_writers(pipelines)  # the PR's title and description, before the Packager
    review = graph.review_line(cfg.catalog)
    if review:
        pipelines.append(review)
    pipelines = _with_plan_line(cfg, pipelines)
    graph.add_storages(pipelines)  # each source but typed specs comes from a storage
    return pipelines


def _with_plan_line(cfg: Config, pipelines: list[dict]) -> list[dict]:
    """Add the Planning line under the others if no pipeline takes PRDs yet (once: after you
    save your pipelines in the game it's part of them, and you can move or delete it)."""
    if any("prd" in graph.sources_of(p) for p in pipelines) or cfg.prd is None:
        return pipelines
    plan = graph.plan_line(cfg.catalog, pipelines)
    if plan is None or any(p.get("id") == plan["id"] for p in pipelines):
        return pipelines
    return pipelines + [plan]


ON_LINE_STATES = ("waiting", "running", "moving", "awaiting_approval")  # what a line's max_crates counts
LINE_ROOM_POLL_SECONDS = 2.0  # how often a crate waiting for room on a full line looks again
SNAPSHOT_LOG_LINES = 60
CARRY_SECONDS = 2.5  # a worker carrying a crate to the machine it needs
MAX_CARRIES = 3  # then it's rolled back instead  # lines of each crate's log in the snapshot


def _midnight() -> float:
    """Local midnight today, as a timestamp: the day's budget starts then."""
    now = datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class Factory:
    def __init__(self, cfg: Config, store: Store, hub: Hub):
        self.cfg = cfg
        self.store = store
        self.hub = hub
        self.layout: list[dict] = []  # every pipeline, as saved, in order
        self.split_turns: dict[str, int] = {}  # splitter id -> crates it has sent out so far
        self.problems: dict[str, str] = {}  # pipelines that can't run (paused): id -> why
        self.lines = self._use(load_pipelines(cfg))
        for pid, why in self.problems.items():
            print(f"[factory] this line can't run until you fix it in build mode: {why}")
        self.locks = self._make_locks()  # one crate per machine at a time
        self.tasks: set[asyncio.Task] = set()
        self.notifiers: list[Notifier] = []
        self.decisions: dict[int, asyncio.Future] = {}  # crates waiting at a gate
        self.outputs: dict[int, list[str]] = {}  # each crate's output from its current step
        self.no_rework: set[int] = set()  # crates whose failing step said NO_REWORK
        self.failed_at: dict[int, int] = {}  # crate -> index of the step that failed last
        self.run_spend: dict[int, list] = {}  # crate -> [Claude cost, turns] of its current machine run
        self.wrong_machine: dict[int, str] = {}  # crate -> machine type its last Claude reply asked for
        self.carried: dict[int, int] = {}  # crate -> times a worker carried it to another machine
        self.retried: dict[tuple[int, str], int] = {}  # (crate, machine) -> times it ran it again by itself
        self.budget_ok: set[int] = set()  # crates you ran again after they went over their budget
        self._budget_alerted = 0.0  # the day the "budget spent" alert last went out
        self.health: AgentHealth | None = None  # set by main; None means never pause
        self.outages: dict[str, Outage] = {}  # broken tools by key (gh, git, network, ...)
        self._tool_wake = asyncio.Event()
        self.prepared: set[tuple[str, str]] = set()  # repos cloned or fetched this run
        self.layout_version = 0  # goes up with each saved layout
        self._layout_changed = asyncio.Event()
        self.pauses = _read_pauses()  # what you paused: the factory, lines, machines
        self.belt_speed = _read_belt_speed()  # cells a second: crates ride belts at the belts' own speed
        self._pause_changed = asyncio.Event()
        self._tool_task: asyncio.Task | None = None

    # Public ----------------------------------------------------------------
    def _use(self, pipelines: list[dict]) -> dict[str, Line]:
        """Take a checked layout: remember all of it, and build lines for the ones that run."""
        _, self.problems = graph.check(self.cfg.catalog, pipelines)
        self.layout = pipelines
        self.views: dict[tuple[str, str], Line] = {}  # lines along their sorters' outputs
        return build_lines(self.cfg, [p for p in pipelines if p["id"] not in self.problems], pipelines)

    def _paused_view(self, p: dict) -> dict:
        """A paused pipeline as the game draws it: as saved, with its machines' catalogue details."""
        p = dict(p)
        p["sources"] = graph.sources_of(p)
        machines = []
        for m in p.get("machines", []):
            base = self.cfg.catalog.get(m.get("type"))
            summary = base.summary() if base else {}
            if base and isinstance(m.get("options"), dict):
                summary["options"] = {**base.options, **{k: bool(v) for k, v in m["options"].items()
                                                         if k in base.options}}
            machines.append({**summary, **{k: v for k, v in m.items() if v not in (None, "") and k != "options"}})
        p["machines"] = machines
        p["order"] = []
        p["repo"] = graph.repo_settings(p)
        p["repo_info"] = config.resolve_repo(self.cfg, p.get("repo")).summary()
        p["paused"] = True
        p["problem"] = self.problems.get(p["id"], "")
        return p

    def snapshot(self) -> dict:
        pipelines = []
        for saved in self.layout:
            line = self.lines.get(saved["id"])
            if line is None:
                pipelines.append(self._paused_view(saved))
                continue
            p = dict(line.graph)
            p.pop("source", None)
            p["sources"] = line.sources
            summaries = {m.id: m.summary() for m in (line.everyone or {}).values()}
            summaries.update({m.id: m.summary() for m in line.machines})
            # Each machine as placed, plus what the catalogue knows (sprite, steps, has_prompt...).
            p["machines"] = [{**summaries[m["id"]], **{k: v for k, v in m.items() if v not in (None, "") and k != "options"}}
                             for m in line.graph["machines"]]
            p["order"] = [m.id for m in line.machines]
            p["repo"] = graph.repo_settings(line.graph)
            p["repo_info"] = line.repo.summary()
            pipelines.append(p)
        return {
            "type": "snapshot",
            "seq": self.store.last_seq(),
            "pauses": self.pauses,
            "layout": {
                "pipelines": pipelines,
                "catalog": [m.summary() for m in self.cfg.catalog.values()],
                "sources": list(graph.SOURCES),
                "belt_seconds": self.cfg.travel_seconds,
                "belt_speed": self.belt_speed,
                "max_rework": self.cfg.max_rework,
                "default_repo": self.cfg.default_repo.summary(),
            },
            "items": self._snapshot_items(),
            "agent": self.health.status() if self.health else {"available": True, "enabled": False},
            "budget": self.budget(),
            "tools": self.tool_status(),
        }

    def _snapshot_items(self) -> list[dict]:
        """Crates for the snapshot: the end of each log only (the game fetches the rest when
        you open a crate), with what each has cost so far."""
        costs = self.store.item_costs()
        items = self.store.list_items(log_tail=SNAPSHOT_LOG_LINES)
        for item in items:
            cost, seconds = costs.get(item["id"], (0.0, 0.0))
            item["cost_usd"] = round(cost, 4)
            item["machine_seconds"] = round(seconds, 1)
        return items

    def stats(self, days: int = 7) -> dict:
        names: dict[str, dict] = {}
        for p in self.layout:
            pid = str(p.get("id", ""))
            names[pid] = {"name": str(p.get("name", "") or pid)}
            for m in p.get("machines", []):
                if isinstance(m, dict):
                    mc = self.cfg.catalog.get(str(m.get("type", "")))
                    names[str(m.get("id", ""))] = {"name": str(m.get("name", "") or (mc.name if mc else m.get("id"))),
                                                   "pipeline": pid, "pipeline_name": names[pid]["name"]}
        return {**stats.compute(self.store, names, days), "budget": self.budget()}

    def line_for_source(self, source: str) -> Line | None:
        """The pipeline a new crate from this source goes to. Typed specs, GitHub issues and
        Jira issues fall back to the pipeline that takes typed specs if none takes them (typed
        specs to the first pipeline if none does)."""
        for line in self.lines.values():
            if source in line.sources and source not in graph.queries_of(line.graph):
                return line  # (a line with its own search for these takes only what it finds)
        if source in ("github", "jira") and any("storages" in line.graph for line in self.lines.values()):
            return None  # issues come in only from a storage belted into an intake
        if source in ("game", "github", "jira"):
            for line in self.lines.values():
                if "game" in line.sources:
                    return line
            # No line takes typed specs: a spec you type goes to the first line, but issues
            # don't go anywhere no line ticked (a PR review line shouldn't get your issues).
            return next(iter(self.lines.values()), None) if source == "game" else None
        return None

    async def submit(self, text: str, source: str = "game", source_ref: str = "", source_url: str = "",
                     pr_url: str = "", head_sha: str = "", pipeline: str = "", repo: str = "",
                     storage: str = "", manual: bool = False) -> dict | None:
        """Put a new crate on a pipeline's intake. Returns the item, or None if no pipeline
        takes crates from this source, or its line is full and the crate came in by itself
        (typed specs and ones you pull in wait at the intake instead). repo: the GitHub repo
        (owner/name) it's worked on, when its storage reads another repo than the line's;
        storage: the storage it came out of."""
        line = self.lines.get(pipeline) if pipeline else self.line_for_source(source)
        if line is None:
            return None
        if source != "game" and not manual and self.line_full(line.id):
            return None  # left where it is (not marked seen): it comes in when there's room
        item = self.store.create_item(text, source, source_ref, source_url, pipeline=line.id,
                                      location=line.at("intake"), pr_url=pr_url, head_sha=head_sha)
        if (repo and repo.lower() != (line.repo.github or "").lower()) or storage:
            fields = {"storage": storage}
            if repo and repo.lower() != (line.repo.github or "").lower():
                fields["repo"] = repo
            self.store.update_item(item["id"], **fields)
            item = self.store.get_item(item["id"])
        await self.hub.emit({"type": "item_created", "item": item})
        await self._notify(item["id"], "picked", "")
        self._spawn(self._process(item["id"]))
        return item

    def busy(self) -> list[int]:
        """Crates still somewhere in the line (including waiting for you)."""
        return [i["id"] for i in self.store.list_items() if i["state"] not in ("shipped", "scrapped")]

    async def set_pipelines(self, pipelines: list) -> str | None:
        """Replace every pipeline with ones built in the game. Returns an error message, or None."""
        pipelines = graph.migrate(pipelines)
        if isinstance(pipelines, list):
            pipelines = [normalize_repo(self.cfg, p) if isinstance(p, dict) else p for p in pipelines]
        graph.rehome(pipelines)
        error, _ = graph.check(self.cfg.catalog, pipelines)
        if error:
            return error
        clean = [_clean_pipeline(p) for p in pipelines]
        old = (self.layout, self.problems)
        lines = self._use(clean)
        error = await self.prepare_repos(lines.values())
        if error:
            self.layout, self.problems = old
            return error
        self.lines = lines
        old_locks = self.locks
        self.locks = {mid: old_locks.get(mid) or lock for mid, lock in self._make_locks().items()}
        # Crates already on the line carry on along the new layout from the next machine they
        # go into (see _relayout).
        self.layout_version += 1
        self._layout_changed.set()
        self._layout_changed = asyncio.Event()
        graph.write(clean)
        await self.hub.broadcast(self.snapshot())
        return None

    async def set_line(self, entries: list) -> str | None:
        """Older games: replace the first pipeline's machines with a straight line."""
        error = config.validate_line(self.cfg.catalog, entries)
        if error:
            return error
        first = next(iter(self.lines.values()))
        replaced = graph.from_line(entries, first.id, first.name)
        replaced["sources"] = first.sources
        return await self.set_pipelines([replaced if p.id == first.id else p.graph for p in self.lines.values()])

    async def prepare_repos(self, lines) -> str | None:
        """Clone or fetch each line's repo, filling in its base branch if it wasn't set (once per
        repo while the backend runs: saving the layout again doesn't touch repos crates are
        working in). Returns what went wrong, or None."""
        for line in lines:
            for repo in [line.repo, *self._storage_repos(line)]:
                if _repo_key(repo) in self.prepared:
                    continue
                try:
                    notes = await asyncio.wait_for(asyncio.to_thread(workspace.ensure_repo, self.cfg, repo),
                                                   REPO_PREP_SECONDS)
                except asyncio.TimeoutError:
                    return (f"{line.name}: getting {repo.github or 'its repo'} ready took over "
                            f"{REPO_PREP_SECONDS:.0f}s (is git waiting for a sign-in?)")
                except Exception as exc:
                    return f"{line.name}: couldn't get {repo.github or 'its repo'} ready ({exc})"
                self.mark_prepared(repo)
                for note in notes:
                    print(f"[factory] {line.name}: {note}")
        return None

    def mark_prepared(self, repo: RepoConfig) -> None:
        self.prepared.add(_repo_key(repo))

    def _storage_repos(self, line: Line) -> list[RepoConfig]:
        """The repos a line's storages read issues and PRs from, other than its own: crates
        from them are worked on in their repo."""
        out = []
        for st in graph.fed_storages(line.graph):
            name = graph.github_name(str(st.get("repo") or "")) if st.get("source") in graph.WORK_REPO_SOURCES else ""
            if name and name.lower() != (line.repo.github or "").lower():
                out.append(self.repo_named(name))
        return out

    def repo_named(self, name: str) -> RepoConfig:
        """A GitHub repo (owner/name) a storage reads, as the factory works on it: cloned under
        the workspace, branching from its default branch."""
        cache = self.__dict__.setdefault("_named_repos", {})
        if name.lower() not in cache:
            cache[name.lower()] = config.resolve_repo(self.cfg, {"github": name})
        return cache[name.lower()]

    def bin_rules(self, location: str) -> dict:
        """The rules of the scrap bin at this location ("build:scrap")."""
        pid, _, place = str(location or "").rpartition(":")
        for p in self.layout:
            if p.get("id") == pid:
                raw = p.get(place) if place == "scrap" else next(
                    (b for b in p.get("bins", []) if isinstance(b, dict) and b.get("id") == place), None)
                return bin_rules(raw)
        return bin_rules(None)

    def repos(self) -> list[RepoConfig]:
        """Every repo the lines work on (the default one first), each once."""
        out = {self.cfg.default_repo.path: self.cfg.default_repo}
        for line in self.lines.values():
            for repo in [line.repo, *self._storage_repos(line)]:
                out.setdefault(repo.path, repo)
        return list(out.values())

    def repo_of(self, item: dict) -> RepoConfig:
        """The repo a crate is worked on: its storage's, when that reads another repo than its
        line's (one line can serve several repos), otherwise its line's."""
        line = self._line_of(item)
        name = str(item.get("repo") or "")
        if name and name.lower() != (line.repo.github or "").lower():
            return self.repo_named(name)
        return line.repo

    def _make_locks(self) -> dict[str, asyncio.Lock]:
        return {m.id: asyncio.Lock() for line in self.lines.values()
                for m in [*line.machines, *(line.everyone or {}).values()]}

    def _line_of(self, item: dict) -> Line:
        """The line a crate is on, along the way its sorters sent it."""
        line = self.lines.get(item.get("pipeline", "")) or next(iter(self.lines.values()), None)
        if line is None:
            raise RuntimeError("the factory has no running pipelines")
        route = graph.parse_route(item.get("route", ""))
        return self._view(line, route) if route else line

    def _view(self, line: Line, route: dict[str, str]) -> Line:
        key = (line.id, graph.route_text(route))
        if key not in self.views:
            view = line_view(self.cfg, line.graph, self.layout, route)
            view.everyone = line.everyone
            self.views[key] = view
        return self.views[key]

    # Pausing ---------------------------------------------------------------
    def _owner_of(self, machine_id: str) -> str:
        for p in self.layout:
            if any(isinstance(m, dict) and m.get("id") == machine_id for m in p.get("machines", [])):
                return str(p.get("id", ""))
        return ""

    def paused_why(self, line_id: str, machine: MachineConfig | None = None) -> str:
        """Why a crate on this line can't go into this machine now ("" when it can)."""
        if self.pauses["factory"]:
            return "the factory is paused"
        lines = set(self.pauses["lines"])
        if line_id in lines:
            return "its line is paused"
        if machine is not None:
            owner = self._owner_of(machine.id)
            if owner and owner != line_id and owner in lines:
                return f"the line the {machine.name} is on is paused"
            if machine.id in self.pauses["machines"]:
                return f"the {machine.name} is paused"
        return ""

    def intake_paused(self, line_id: str) -> bool:
        """Whether a line's storages stop bringing crates in (the factory or the line paused,
        or the line full)."""
        return self.pauses["factory"] or (bool(line_id) and line_id in self.pauses["lines"]) or \
            (bool(line_id) and self.line_full(line_id))

    def on_line(self, line_id: str) -> int:
        """Crates on a line past its intake: on belts, in machines, or at a gate waiting for you.
        Held crates don't count (they're off to the side, blocking nothing)."""
        return self.store.count_on_line(line_id, ON_LINE_STATES)

    def line_limit(self, line_id: str) -> int:
        line = self.lines.get(line_id)
        return graph.max_crates(line.graph) if line is not None else 0

    def line_full(self, line_id: str) -> bool:
        limit = self.line_limit(line_id)
        return limit > 0 and self.on_line(line_id) >= limit

    async def _wait_for_room(self, item_id: int, line: Line) -> Line | None:
        """A new crate waits at the intake while its line has its most crates on it. Returns the
        line it then starts on (a new layout may have changed it), or None if it's gone."""
        told = False
        while True:
            item = self.store.get_item(item_id)
            if not item or item.get("state") in ("shipped", "scrapped"):
                return None
            line = self.lines.get(str(item.get("pipeline", ""))) or line
            if not self.line_full(line.id):
                break
            if not told:
                told = True
                limit = self.line_limit(line.id)
                await self._set_waiting(item_id, "intake", f"the line is full ({limit} crate{'s' if limit != 1 else ''} "
                                        "on it); it starts when one leaves")
            await asyncio.sleep(LINE_ROOM_POLL_SECONDS)
        if told:
            await self._set_waiting(item_id, "intake", "")
        return line

    async def set_paused(self, target: str, target_id: str, paused: bool) -> str | None:
        """Pause or resume the factory, a line or a machine. Crates finish the machine they're
        in, then wait on the belt in front of the next paused one. Returns an error, or None."""
        if target == "factory":
            self.pauses["factory"] = bool(paused)
        elif target in ("line", "machine"):
            key = "lines" if target == "line" else "machines"
            known = {str(p.get("id", "")) for p in self.layout} if target == "line" else \
                {str(m.get("id", "")) for p in self.layout for m in p.get("machines", []) if isinstance(m, dict)}
            if target_id not in known and not (target_id in self.pauses[key] and not paused):
                return f"there's no {target} {target_id!r}"
            ids = [i for i in self.pauses[key] if i != target_id]
            self.pauses[key] = ids + [target_id] if paused else ids
        else:
            return f"can't pause {target!r}"
        _write_pauses(self.pauses)
        self._pause_changed.set()
        self._pause_changed = asyncio.Event()
        await self.hub.broadcast({"type": "pauses", "pauses": self.pauses})
        return None

    async def _wait_unpaused(self, item_id: int, line: Line, machine: MachineConfig) -> bool:
        """Hold the crate on the belt in front of a paused machine (or on a paused line, or in a
        paused factory) until it's resumed. False if a new layout was saved meanwhile (the
        machine may be gone: the crate finds its place again)."""
        why = self.paused_why(line.id, machine)
        if not why:
            return True
        await self._log(item_id, machine.id, f"[factory] {why}; waiting here until it's resumed")
        version = self.layout_version
        while why:
            paused = asyncio.ensure_future(self._pause_changed.wait())
            changed = asyncio.ensure_future(self._layout_changed.wait())
            try:
                await asyncio.wait([paused, changed], return_when=asyncio.FIRST_COMPLETED)
            finally:
                paused.cancel()
                changed.cancel()
            if version != self.layout_version:
                return False
            why = self.paused_why(line.id, machine)
        await self._log(item_id, machine.id, "[factory] resumed; carrying on")
        return True

    async def _relayout(self, item_id: int, old: list[MachineConfig], i: int) -> tuple[Line, int] | None:
        """A layout was saved while this crate was on its line, heading into old[i] (or to the
        dock when i is past the end). Where it carries on in the new layout: (line, index of the
        machine it goes into next). That machine if it's still on the line; otherwise the next
        one along its old way that is; otherwise the start of the line. A crate whose line is
        gone or can't run waits, held, until a layout has it again (or you scrap it). Returns
        None when you scrapped it."""
        while True:
            item = self.store.get_item(item_id)
            if self.lines.get(item.get("pipeline", "")) is not None:
                break
            if await self._park(item_id, "Its line was removed or can't run in the new layout; it waits here "
                                "until the line is back (fix it in build mode)."):
                return None
        try:
            line = self._line_of(item)
        except Exception:
            self.store.update_item(item_id, route="")  # its sorters' choices don't fit any more
            line = self.lines[item["pipeline"]]
        index = {m.id: k for k, m in enumerate(line.machines)}
        if i >= len(old):
            return line, len(line.machines)
        for j in range(i, len(old)):
            if old[j].id in index:
                if j != i:
                    await self._log(item_id, "factory", f"[factory] the {old[i].name} it was heading to isn't on its "
                                    f"line any more; carrying on at the {old[j].name}")
                return line, index[old[j].id]
        await self._log(item_id, "factory", f"[factory] the {old[i].name} it was heading to isn't on its line any "
                        "more; going back to the start of the line")
        return line, 0

    async def _when_line_back(self, item_id: int) -> None:
        """After a restart, a crate whose line can't run: held until it can, then on from where it was."""
        while self.lines.get(self.store.get_item(item_id).get("pipeline", "")) is None:
            if await self._park(item_id, "Its line was removed or can't run; it waits here until the line is "
                                "back (fix it in build mode), or scrap it."):
                return
        item = self.store.get_item(item_id)
        try:
            line = self._line_of(item)
        except Exception:
            self.store.update_item(item_id, route="")
            line = self.lines[item["pipeline"]]
        start, _ = _resume_point(line, str(item.get("location", "")))
        await self._process(item_id, start=start, rework=self._has_worktree(item_id))

    async def _park(self, item_id: int, why: str) -> bool:
        """Hold a crate where it is until the layout changes or you decide. True if you scrapped it."""
        self.store.update_item(item_id, waiting_for=why[:400])
        item = self.store.get_item(item_id)
        await self._move(item_id, str(item.get("location", "")), 0.0, state="held")
        await self._log(item_id, "factory", f"[factory] {why}")
        future = asyncio.get_running_loop().create_future()
        self.decisions[item_id] = future
        changed = asyncio.ensure_future(self._layout_changed.wait())
        try:
            await asyncio.wait([future, changed], return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.decisions.pop(item_id, None)
            changed.cancel()
        self.store.update_item(item_id, waiting_for="")
        if future.done() and future.result() == "scrap":
            await self._scrap(item_id, "scrapped by you while its line was gone", "", "blocked")
            return True
        if not future.done():
            future.cancel()
        await self._move(item_id, str(item.get("location", "")), 0.0, state="waiting")
        return False

    def _has_worktree(self, item_id: int) -> bool:
        """The crate's branch and worktree were made already (so Make branch mustn't run again)."""
        return (self.cfg.worktrees / f"crate-{item_id}").is_dir()

    def recover_interrupted(self) -> tuple[list[int], list[int]]:
        """After a restart, crates the last run left on a belt or in a machine carry on from
        the machine they were at (it runs again from its first step), or from the intake.
        Crates whose line is gone or can't run wait, held, until it's back. Returns (resumed
        ids, scrapped ids): nothing is scrapped any more."""
        resumed, scrapped = [], []
        machine_ids = {m.id for line in self.lines.values() for m in [*line.machines, *(line.everyone or {}).values()]}
        parked = [i for i in self.store.list_items() if i["state"] == "held" and i.get("location") not in machine_ids]
        for item in self.store.in_flight() + parked:
            item_id = item["id"]
            if self.lines.get(item.get("pipeline", "")) is None:
                # Its line is gone or can't run: it waits, held, until a layout has it again.
                self._spawn(self._when_line_back(item_id))
                resumed.append(item_id)
                continue
            try:
                line = self._line_of(item)
            except Exception:
                line = self.lines[item["pipeline"]]
            start, where = _resume_point(line, str(item.get("location", "")))
            self.store.append_log(item_id, f"[factory] the backend stopped while this crate was {where}; "
                                  "carrying on from there")
            self._spawn(self._process(item_id, start=start, rework=self._has_worktree(item_id)))
            resumed.append(item_id)
        return resumed, scrapped

    def resume_waiting(self) -> int:
        """After a restart, put crates that were waiting at a gate back in front of it (not on a
        paused pipeline: they wait until it runs again)."""
        count = 0
        for item in self.store.list_items():
            if self.lines.get(item.get("pipeline", "")) is None:
                continue
            line = self._line_of(item)
            index = {m.id: i for i, m in enumerate(line.machines)}
            if item["state"] == "awaiting_approval" and item["location"] in index:
                self._spawn(self._process(item["id"], start=index[item["location"]], resume_gate=True))
                count += 1
            elif item["state"] == "held" and item["location"] in index:
                self._spawn(self._process(item["id"], start=index[item["location"]], resume_hold=True))
                count += 1
        return count

    async def decide(self, item_id: int, decision: str, note: str = "") -> str | None:
        """Approve, reject (send back for rework, or scrap if the machine has nowhere to send
        it) or scrap a crate waiting at a gate. Returns an error message, or None."""
        future = self.decisions.get(item_id)
        if future is None or future.done():
            return f"crate #{item_id} is not waiting for approval"
        held = (self.store.get_item(item_id) or {}).get("state") == "held"
        if held and decision not in ("retry", "scrap"):
            return f"crate #{item_id} is held: run it again or scrap it"
        if not held and decision == "retry":
            return f"crate #{item_id} is waiting for approval, not held"
        if note:
            self.store.update_item(item_id, notes=note)
        future.set_result(decision)
        return None

    async def retry(self, item_id: int, note: str = "") -> str | None:
        """Send a scrapped crate back to the start of the line. Returns an error message, or None."""
        try:
            item = self.store.get_item(item_id)
        except TypeError:
            return f"no crate #{item_id}"
        for key in [k for k in self.retried if k[0] == item_id]:
            del self.retried[key]  # Run again: each machine's retries count from zero again
        if item["state"] == "held":
            return await self.decide(item_id, "retry", note)
        if item["state"] != "scrapped":
            return f"crate #{item_id} is not in the scrap bin"
        line = self.lines.get(item.get("pipeline", ""))
        if line is None:
            return (f"crate #{item_id}'s pipeline is paused or gone: fix it in build mode, then run it again"
                    if self.lines else "the factory has no running pipelines")
        self.store.update_item(item_id, notes=note, review="", state="queued", loops=0, route="", pr_title="", pr_body="",
                               location=line.at("intake"), risk="", risk_reason="", outcome="", scrap_kind="")
        await self._stamp_risk(item_id, "", "")
        if item.get("source_ref") and item.get("source") in ("github", "jira"):
            self.store.mark_seen(item["source"], item["source_ref"], item_id)  # it's ours again
            await self._notify(item_id, "picked", "")
        await self._log(item_id, "factory", "== Retry ==" + (f" Notes: {note}" if note else ""))
        await self._cleanup_branch(item_id)
        self._spawn(self._process(item_id))
        return None

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # Pipeline --------------------------------------------------------------
    async def set_belt_speed(self, speed) -> str | None:
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            return "belt speed needs a number"
        self.belt_speed = min(BELT_SPEEDS[1], max(BELT_SPEEDS[0], speed))
        try:
            BELTS_FILE.write_text(json.dumps({"speed": self.belt_speed}), encoding="utf-8")
        except OSError as exc:
            print(f"[factory] could not save the belt speed: {exc}")
        await self.hub.broadcast({"type": "belt_speed", "speed": self.belt_speed})
        return None

    def _belt_seconds(self, item_id: int, to: str) -> float | None:
        """How long a crate takes along the belt at `to` ("<line>:belt_<i>") at the belts' speed,
        from the length of the belt you laid; None when it can't tell (no such belt)."""
        place = to.rpartition(":")[2]
        if not place.startswith("belt_"):
            return None
        try:
            i = int(place[len("belt_"):])
            item = self.store.get_item(item_id)
            line = self._line_of(item)
        except Exception:
            return None
        if line.belt(i) != to:
            return None
        if i == 0:
            src, port = graph.qualify(line.id, "intake"), "out"
        elif i - 1 < len(line.machines):
            prev = line.machines[i - 1]
            src = prev.id
            port = graph.parse_route(str(item.get("route", ""))).get(prev.id, "pass")
        else:
            return None
        for p in self.layout:
            pid = str(p.get("id", ""))
            for b in p.get("belts", []):
                if isinstance(b, dict) and graph.qualify(pid, str(b.get("from", ""))) == src and b.get("port") == port:
                    path = b.get("path")
                    if not isinstance(path, list) or not path:
                        return None
                    return max(1, len(path)) / self.belt_speed
        return None

    async def _move(self, item_id: int, to: str, duration: float = 0.0, state: str | None = None,
                    wait: bool = True) -> None:
        if duration > 0 and ":belt_" in to:
            duration = self._belt_seconds(item_id, to) or duration
        fields = {"location": to}
        if state:
            fields["state"] = state
        self.store.update_item(item_id, **fields)
        event = {"type": "item_moved", "item_id": item_id, "to": to, "duration": duration}
        if state:
            event["state"] = state
        await self.hub.emit(event)
        if duration and wait:
            await asyncio.sleep(duration)

    async def _log(self, item_id: int, machine: str, line: str) -> None:
        self.store.append_log(item_id, line)
        await self.hub.emit({"type": "job_log", "item_id": item_id, "machine": machine, "line": line})

    async def _process(self, item_id: int, start: int = 0, resume_gate: bool = False,
                       resume_hold: bool = False, rework: bool = False) -> None:
        travel = self.cfg.travel_seconds
        line = self._line_of(self.store.get_item(item_id))
        if start == 0 and not resume_gate and not resume_hold and graph.max_crates(line.graph) > 0:
            if await self._wait_for_room(item_id, line) is None:
                return
            line = self._line_of(self.store.get_item(item_id))
        machines = line.machines
        belt = line.belt
        if start == 0 and not resume_gate:
            await self._move(item_id, belt(0), travel, state="waiting")

        i = start
        # True while the crate is back for rework (skips first_pass_only steps such as Make
        # branch); also for a crate picked up again whose branch was already made.
        if resume_hold:
            item = self.store.get_item(item_id)
            if await self._hold(item_id, line, i, str(item.get("scrap_kind") or "work_failed"),
                                str(item.get("waiting_for") or "held after a restart"), "") != "retry":
                return
            rework = True
        version = self.layout_version
        while i < len(machines):
            if version != self.layout_version:
                version = self.layout_version
                moved = await self._relayout(item_id, machines, i)
                if moved is None:
                    return
                line, i = moved
                machines, belt = line.machines, line.belt
                version = self.layout_version
                # Onto the new layout's belt into the machine it goes to next (or the dock), and
                # along it, rather than jumping straight there.
                if str(self.store.get_item(item_id).get("location", "")) != belt(i):
                    await self._move(item_id, belt(i), travel, state="waiting" if i < len(machines) else "moving")
                if i >= len(machines):
                    break
            machine = machines[i]
            if not (resume_gate and i == start):
                if not await self._wait_unpaused(item_id, line, machine):
                    continue  # a new layout came while it waited: find its place in it
            if machine.kind == "merger":
                # Straight through: every belt in, one belt out.
                await self._move(item_id, machine.id, 0.3, state="running")
                await self._move(item_id, belt(i + 1), travel, state="waiting" if i + 1 < len(machines) else "moving")
                i += 1
                continue
            if machine.sorter:
                # Straight through: it picks an output, and the crate carries on along it.
                await self._move(item_id, machine.id, 0.3, state="running")
                port, why = await self._sort(item_id, machine)
                if port is None:
                    if await self._hold(item_id, line, i, "blocked", f"{machine.name}: {why}.", "") != "retry":
                        return
                    continue
                route = graph.parse_route(self.store.get_item(item_id).get("route", ""))
                route[machine.id] = port
                self.store.update_item(item_id, route=graph.route_text(route))
                await self._log(item_id, machine.id, f"[factory] {machine.name}: out of output {port[-1]} ({why})")
                line = self._line_of(self.store.get_item(item_id))
                machines, belt = line.machines, line.belt
                await self._move(item_id, belt(i + 1), travel,
                                 state="waiting" if i + 1 < len(machines) else "moving")
                i += 1
                continue
            last = i == len(machines) - 1
            next_state = "moving" if last else "waiting"
            back_to: int | None = None
            failed: tuple[str, str, str] | None = None  # (kind, reason, output) when it can't go back
            gate_output = ""  # what the machine said, handed on if you send the crate back
            if not (resume_gate and i == start):
                if machine.uses_agent():
                    over = await self._check_budget(item_id, line, i, machine)
                    if over == "relayout" or over == "retry":
                        continue
                    if over:
                        return
                    await self._wait_for_agent(item_id, machine)
                await self._wait_for_tools(item_id, machine)
                lock = contextlib.nullcontext() if machine.parallel else self.locks[machine.id]
                async with lock:
                    await self._move(item_id, machine.id, 0.3, state="running")
                    ok, failed_step, output = await self._run_machine(item_id, machine, rework)
                    gate_output = output
                    wanted = self.wrong_machine.pop(item_id, "")
                    if wanted:
                        self.no_rework.discard(item_id)  # not its work: a worker takes it elsewhere
                    elif not ok:
                        # A step that printed NO_REWORK failed in a way rework can't fix (a closed PR).
                        no_rework = item_id in self.no_rework
                        self.no_rework.discard(item_id)
                        back_to = None if no_rework else await self._rework_target(item_id, line, machine)
                        if back_to is None:
                            kind = triage.scrap_kind(str(self.store.get_item(item_id).get("outcome", "")), False)
                            failed = (kind, f"{machine.name}: {failed_step}", output)
                        else:
                            await self._send_back(item_id, line, i, back_to, f"{failed_step} failed", output)
                    elif not machine.gate:
                        # Leave the machine before releasing it, so the next crate can enter.
                        await self._move(item_id, belt(i + 1), travel, state=next_state, wait=False)
                if wanted:
                    moved = await self._redirect(item_id, line, i, wanted, output)
                    if moved is None:
                        return  # rolled back into the scrap bin
                    i, rework = moved
                    continue
                if failed is not None and self._leads_on(line, i):
                    # Its red belt leads on: the crate carries on along it.
                    line = await self._take_red_belt(item_id, line, i, *failed)
                    machines, belt = line.machines, line.belt
                    i, rework = i + 1, False
                    continue
                if failed is not None:
                    # Out of the machine (so the next crate can use it): scrapped, or held here.
                    if await self._scrap_or_hold(item_id, line, i, *failed) != "retry":
                        return
                    # You pressed Run again: steps that already ran once (Make branch) are skipped.
                    rework = rework or any(st.first_pass_only for st in machine.steps[:self.failed_at.get(item_id, 0)])
                    continue
                if back_to is not None:
                    await self._ride_back(item_id, line, i, back_to)
                    # Again at the same machine: skip Make branch only if it got that far.
                    i, rework = back_to, (True if back_to != i else rework or self._has_worktree(item_id))
                    continue
                if not machine.gate:
                    await asyncio.sleep(self._belt_seconds(item_id, belt(i + 1)) or travel)
                    i += 1
                    continue

            # Gate: the crate waits here (without blocking the machine) until you decide.
            decision = await self._await_decision(item_id, machine)
            if decision == "approve":
                await self._log(item_id, machine.id, f"[factory] approved at {machine.name}")
                await self._move(item_id, belt(i + 1), travel, state=next_state)
                i += 1
                continue
            back_to = await self._rework_target(item_id, line, machine) if decision == "reject" else None
            if back_to is None:
                if decision == "scrap":  # you scrapped it yourself: always allowed
                    await self._reject(item_id, machine)
                    return
                note = str(self.store.get_item(item_id).get("notes", "") or "")
                if self._leads_on(line, i):
                    line = await self._take_red_belt(item_id, line, i, "rejected", f"{machine.name}: rejected", note)
                    machines, belt = line.machines, line.belt
                    i, resume_gate, rework = i + 1, False, False
                    continue
                if await self._scrap_or_hold(item_id, line, i, "rejected", f"{machine.name}: rejected",
                                             note, machine=machine) != "retry":
                    return
                rework, resume_gate = True, False
                continue
            await self._send_back(item_id, line, i, back_to, "sent back by you",
                                  self._gate_feedback(item_id, machine, gate_output))
            await self._ride_back(item_id, line, i, back_to)
            i, resume_gate, rework = back_to, False, True

        if version != self.layout_version:  # the layout changed on its last stretch
            moved = await self._relayout(item_id, machines, len(machines))
            if moved is None:
                return
            line = moved[0]
            if moved[1] < len(line.machines):  # the new layout has more for it to do
                self._spawn(self._process(item_id, start=moved[1]))
                return
            machines = line.machines
            if str(self.store.get_item(item_id).get("location", "")) != line.belt(len(machines)):
                await self._move(item_id, line.belt(len(machines)), travel, state="moving")
        if graph.is_bin_node(line.end):  # its belts end in a scrap bin
            item = self.store.get_item(item_id)
            kind = str(item.get("scrap_kind") or "work_failed")
            await self._scrap(item_id, "its belt runs into the scrap bin", str(item.get("notes", "") or ""), kind,
                              via=line.belt(len(machines)), into=line.end)
            return
        await self._stamp_pr_state(item_id)
        await self._move(item_id, line.at("dock"), 0.3, state="shipped")
        await self._notify(item_id, "shipped", "")

    # Broken tooling ------------------------------------------------------
    def tool_status(self) -> list[dict]:
        return [{"key": k, "label": o.problem.label, "reason": o.problem.reason, "detail": o.detail,
                 "since": o.since, "can_check": o.problem.probe is not None}
                for k, o in self.outages.items()]

    async def _broadcast_tools(self) -> None:
        await self.hub.broadcast({"type": "tool_status", "outages": self.tool_status()})

    async def _hold_for_tool(self, item_id: int, machine: MachineConfig, index: int, step: StepConfig,
                             problem: triage.ToolProblem) -> None:
        outage = self.outages.get(problem.key)
        if outage is None:
            tail = "\n".join(self.outputs.get(item_id, [])[-4:])[-400:]
            outage = self.outages[problem.key] = Outage(problem, tail, time.time(), asyncio.Event())
            print(f"[factory] {problem.label} is broken: {problem.reason}")
            await self._broadcast_tools()
            if self._tool_task is None or self._tool_task.done():
                self._tool_task = asyncio.create_task(self._watch_tools())
        self.outputs.pop(item_id, None)
        await self._log(item_id, machine.id, f"[factory] {problem.reason} '{step.name}' will run again "
                        f"once it's fixed; the crate waits here.")
        await self.hub.emit({"type": "job_progress", "item_id": item_id, "machine": machine.id,
                             "progress": index / len(machine.steps), "step": f"Waiting for {problem.label}"})
        await outage.fixed.wait()
        await self._log(item_id, machine.id, f"[factory] {problem.label} is working again; running '{step.name}' again")

    async def _wait_for_tools(self, item_id: int, machine: MachineConfig) -> None:
        """Hold a crate in front of a machine that needs a tool that's broken right now."""
        needs = triage.tools_of([s.command for s in machine.steps])
        while True:
            broken = [o for k, o in self.outages.items() if k in needs]
            if not broken:
                return
            await self._log(item_id, machine.id, f"[factory] waiting here until {broken[0].problem.label} "
                            f"is fixed: {broken[0].problem.reason}")
            await broken[0].fixed.wait()

    async def check_tools(self) -> None:
        """Check every broken tool now (the game's Try again button). A problem no check can see
        (a crashed script, a config change) counts as fixed when you press it."""
        await self._probe_tools(manual=True)

    async def _watch_tools(self) -> None:
        while self.outages:
            self._tool_wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._tool_wake.wait(), timeout=TOOL_CHECK_SECONDS)
            await self._probe_tools(manual=False)

    async def _probe_tools(self, manual: bool) -> None:
        changed = False
        for key, outage in list(self.outages.items()):
            probe = outage.problem.probe
            if probe is None:
                ok = manual
            else:
                repo = self.cfg.default_repo.path
                cwd = repo if outage.problem.probe_cwd == "repo" and repo.is_dir() else self.cfg.workspace
                try:
                    argv = runner.resolve(probe, self.cfg.allowed_commands)
                    ok = await runner.run(argv, cwd, _ignore_line, timeout=60) == 0
                except runner.CommandNotAllowed:  # a tool the scripts run (pnpm): is it there now?
                    ok = shutil.which(probe[0]) is not None
                except FileNotFoundError:
                    ok = False
            if ok:
                print(f"[factory] {outage.problem.label} is working again")
                del self.outages[key]
                outage.fixed.set()
                changed = True
        if changed or manual:
            await self._broadcast_tools()

    async def _wait_for_agent(self, item_id: int, machine: MachineConfig) -> None:
        """Hold the crate on the belt in front of a Claude machine while Claude is unavailable."""
        if self.health is None or self.health.available:
            return
        await self._log(item_id, machine.id, f"[factory] Claude is unavailable ({self.health.reason.rstrip('.')}); "
                        f"waiting here until it's back")
        await self.health.wait_available()
        await self._log(item_id, machine.id, "[factory] Claude is back; carrying on")

    async def _sort(self, item_id: int, machine: MachineConfig) -> tuple[str | None, str]:
        """(the output a sorter sends this crate out of, why), or (None, why none fits). Rules
        are tried in output order; the first that matches wins."""
        item = self.store.get_item(item_id)
        if machine.kind == "splitter":
            out = graph.all_outputs(self.layout)
            ports = [port for port in graph.SORTER_PORTS if (machine.id, port) in out]
            if not ports:
                return None, "it has no belts out"
            turn = self.split_turns.get(machine.id, 0)
            self.split_turns[machine.id] = turn + 1
            return ports[turn % len(ports)], "taking turns"
        changes: tuple[list[str], int] | None = None
        for rule in sorted(machine.rules, key=lambda r: r["port"]):
            kind, value = rule["when"], rule.get("value", "")
            if kind in ("files", "size") and changes is None:
                changes = await self._changes(item)
            if _rule_matches(kind, value, item, changes):
                return rule["port"], _rule_words(kind, value)
        risk = item.get("risk") or "not rated"
        return None, (f"no output's rule matches this crate (risk {risk}, from {item.get('source') or 'game'}). "
                      f"Change the sorter's rules, or give an output \"everything else\"")

    async def _changes(self, item: dict) -> tuple[list[str], int]:
        """The files a crate's branch changes and how many lines (added + removed), against
        where it branched from; nothing if it has no worktree yet."""
        worktree = self.cfg.worktrees / f"crate-{item['id']}"
        if not worktree.is_dir():
            return [], 0
        repo = self.repo_of(item)
        base = repo.base_branch or "main"
        base_ref = f"origin/{base}" if repo.remote else base

        async def git(*args: str) -> str:
            proc = await asyncio.create_subprocess_exec("git", *args, cwd=worktree, stdout=subprocess.PIPE,
                                                        stderr=subprocess.DEVNULL)
            out, _ = await proc.communicate()
            return out.decode(errors="replace") if proc.returncode == 0 else ""

        fork = (await git("merge-base", base_ref, "HEAD")).strip() or base_ref
        files, lines = [], 0
        for row in (await git("diff", "--numstat", fork)).splitlines():
            parts = row.split("\t")
            if len(parts) == 3:
                files.append(parts[2])
                lines += sum(int(n) for n in parts[:2] if n.isdigit())
        for name in (await git("ls-files", "--others", "--exclude-standard")).splitlines():
            files.append(name)  # new files not added yet
        return files, lines

    async def _rework_target(self, item_id: int, line: Line, machine: MachineConfig) -> int | None:
        """Index of the machine a failed crate goes back to, or None to scrap it."""
        ids = [m.id for m in line.machines]
        earlier = ids[:ids.index(machine.id)] if machine.id in ids else []
        target = machine.on_fail if machine.on_fail in earlier else (machine.id if machine.rework_here else "")
        used = self.retried.get((item_id, machine.id), 0)
        if machine.retries and used < machine.retries and machine.id in ids and machine.on_fail not in earlier:
            # Its own retries come first: then its red belt, a bin or holding it.
            self.retried[(item_id, machine.id)] = used + 1
            await self._log(item_id, machine.id, f"[factory] trying the {machine.name} again "
                            f"(retry {used + 1} of {machine.retries})")
            return ids.index(machine.id)
        if not target:
            return None
        loops = int(self.store.get_item(item_id).get("loops", 0) or 0)
        if loops >= self.cfg.max_rework:
            await self._log(item_id, machine.id,
                            f"[factory] already sent back {loops} times (max_rework); scrapping instead")
            return None
        return ids.index(target)

    def _leads_on(self, line: Line, at: int) -> bool:
        """Does machine `at`'s red belt lead on (not back for rework, not into a bin)?"""
        m = line.machines[at]
        return bool(m.on_fail) and not m.scrap_bin and m.on_fail not in {x.id for x in line.machines[:at]}

    async def _take_red_belt(self, item_id: int, line: Line, at: int, kind: str, reason: str,
                             output: str) -> Line:
        """A failed crate leaves machine `at` along its red belt, onward: it's on a new way through
        (route "<machine>=fail"), with why it failed as its notes. Returns the line it's on now."""
        machine = line.machines[at]
        item = self.store.get_item(item_id)
        route = graph.parse_route(item.get("route", ""))
        route[machine.id] = graph.FAIL
        tail = "\n".join(triage.explanation(output).splitlines()[-25:])[-3000:] if output else ""
        note = f"{triage.KIND_WORDS.get(kind, kind)} at the {machine.name} ({reason})." + (f"\n\n{tail}" if tail else "")
        self.store.update_item(item_id, route=graph.route_text(route), scrap_kind=kind, notes=note)
        await self.hub.emit({"type": "item_triaged", "item_id": item_id, "kind": kind, "reason": reason})
        now = self._line_of(self.store.get_item(item_id))
        onward = now.machines[at + 1].name if at + 1 < len(now.machines) else (
            "the scrap bin" if graph.is_bin_node(now.end) else "the dock")
        await self._log(item_id, machine.id, f"[factory] {triage.KIND_WORDS.get(kind, kind)} ({reason}): "
                        f"out of the red port, on to {onward}")
        await self._move(item_id, now.belt(at + 1), self.cfg.travel_seconds,
                         state="waiting" if at + 1 < len(now.machines) else "moving")
        return now

    def _wrong_machine_hint(self, machine: MachineConfig) -> str:
        """Told to every Claude step: how to say the crate needs another kind of machine."""
        kinds = "\n".join(f"- {t}: {m.name}. {m.description}".rstrip() for t, m in self.cfg.catalog.items()
                          if t != machine.type and m.steps and not m.sorter)
        return ("\n\nIf this crate needs a kind of work you are not built for (for example a pull request "
                "that only needs its conflicts merged, or a code change sent to a reviewer), don't try to do "
                "it some other way. Make no changes and make the first line of your reply exactly\n"
                "Outcome: wrong machine: <type>\nwith <type> one of these, then one line on why:\n" + kinds)

    def _wanted_type(self, reply: str, machine: MachineConfig) -> str:
        m = triage.WRONG_MACHINE_RE.search(reply or "")
        if not m:
            return ""
        said = m.group(1).strip().lower().replace(" ", "_").replace("-", "_")
        for t, mc in self.cfg.catalog.items():
            if said in (t, mc.name.lower().replace(" ", "_")) and t != machine.type:
                return t
        return ""

    async def _redirect(self, item_id: int, line: Line, at: int, wanted: str, output: str) -> tuple[int, bool] | None:
        """The machine at `at` said the crate needs a `wanted` machine. If its line has one, a
        worker carries it there: (index, rework). If not (or it's been passed round too often),
        it's rolled back into the scrap bin: None."""
        machine = line.machines[at]
        want = self.cfg.catalog[wanted]
        reply = str(self.store.get_item(item_id).get("review", "") or "").strip()
        why = "\n".join(l for l in reply.splitlines()[1:] if l.strip())[:600]
        target = next((k for k, m in enumerate(line.machines) if m.type == wanted and k != at), None)
        hops = self.carried.get(item_id, 0)
        if target is not None and hops < MAX_CARRIES:
            self.carried[item_id] = hops + 1
            to = line.machines[target]
            note = f"The {machine.name} passed this crate to you: it needs a {want.name}." + (f"\n\n{why}" if why else "")
            self.store.update_item(item_id, notes=note, location=line.belt(target), state="waiting")
            await self._log(item_id, machine.id, f"[factory] the {machine.name} says this needs a {want.name}: "
                            f"a worker carries it to the {to.name}" + (f" ({why.splitlines()[0]})" if why else ""))
            await self.hub.emit({"type": "item_moved", "item_id": item_id, "to": line.belt(target),
                                 "duration": CARRY_SECONDS, "state": "waiting", "carried": True,
                                 "from": machine.id, "machine": to.id})
            await asyncio.sleep(CARRY_SECONDS)
            return target, self._has_worktree(item_id)
        self.store.update_item(item_id, outcome=f"wrong_machine:{wanted}", notes=why)
        reason = (f"needs a {want.name}, and the {line.name} line has none" if target is None
                  else f"needs a {want.name}, but it has been passed round {hops} times")
        # Along its red belt if that runs into a bin (maybe an extra one), else into the line's bin.
        if machine.scrap_bin:
            await self._scrap(item_id, f"{machine.name}: {reason}", reply, kind="wrong_line",
                              via=line.return_belt(at), into=machine.scrap_to)
        else:
            await self._scrap(item_id, f"{machine.name}: {reason}", reply, kind="wrong_line")
        return None

    async def _send_back(self, item_id: int, line: Line, at: int, to: int, why: str, details: str) -> None:
        """Record why a crate is going back; the target machine gets it as {notes}."""
        machine = line.machines[at]
        target = line.machines[to]
        loops = int(self.store.get_item(item_id).get("loops", 0) or 0) + 1
        note = (f"The {machine.name} sent this crate back ({why}). Your earlier work is already "
                f"committed on this branch: fix the problem below without starting over."
                if to != at else f"This came back to the {machine.name} ({why}).")
        if details.strip():
            note += "\n\n" + details.strip()
        self.store.update_item(item_id, loops=loops, notes=note, outcome="")
        if self.store.get_item(item_id).get("risk"):  # the change will differ: assess it again
            await self._stamp_risk(item_id, "", "")
        await self._log(item_id, machine.id,
                        f"[factory] sending back to {target.name} for rework "
                        f"({loops} of {self.cfg.max_rework})")
        await self._move(item_id, line.return_belt(at), 0.4, state="moving", wait=False)

    async def _ride_back(self, item_id: int, line: Line, at: int, to: int) -> None:
        """Carry a crate along the return belt to the belt in front of machine `to`."""
        await asyncio.sleep(0.4)
        await self._move(item_id, line.return_belt(to), self.cfg.travel_seconds * max(1, at - to) * 0.6,
                         state="moving")
        await self._move(item_id, line.belt(to), 0.4, state="waiting")

    async def _stamp_risk(self, item_id: int, risk: str, reason: str) -> None:
        self.store.update_item(item_id, risk=risk, risk_reason=reason)
        await self.hub.emit({"type": "item_risk", "item_id": item_id, "risk": risk, "reason": reason})

    async def _auto_approves(self, item_id: int, machine: MachineConfig) -> bool:
        """Whether a gate lets the crate through by itself: the stamped risk is no higher than
        the machine's setting."""
        if not machine.auto_approve:
            return False
        risk = str(self.store.get_item(item_id).get("risk", "") or "")
        limit = machine.auto_approve
        if risk not in RISK_ORDER:
            await self._log(item_id, machine.id, "[factory] no risk stamp on this crate, so it needs you to approve")
            return False
        if RISK_ORDER[risk] > RISK_ORDER[limit]:
            await self._log(item_id, machine.id, f"[factory] {risk} risk is above the auto-approve limit "
                            f"({'low' if limit == 'low' else 'low or medium'}), so it needs you to approve")
            return False
        await self._log(item_id, machine.id, f"[factory] the risk is {risk}: "
                        f"approved automatically")
        return True

    async def _await_decision(self, item_id: int, machine: MachineConfig) -> str:
        # Notes from an earlier rework were for the machine that fixed it; from here on they
        # hold only what you (or a GitHub review) say at this gate.
        self.store.update_item(item_id, notes="")
        if await self._auto_approves(item_id, machine):
            await self._notify(item_id, "auto_approved", machine.name)
            return "approve"
        future = asyncio.get_running_loop().create_future()
        self.decisions[item_id] = future
        await self._move(item_id, machine.id, 0.0, state="awaiting_approval")
        on_github = machine.github_review and bool(self.store.get_item(item_id).get("pr_url"))
        where = "in the game" + (" or with a review on the PR" if on_github else "")
        await self._log(item_id, machine.id, f"[factory] waiting at {machine.name} for you to approve or reject {where}")
        await self._notify(item_id, "awaiting_approval", machine.name)
        since = datetime.now(timezone.utc)
        watcher = (asyncio.create_task(self._watch_pr_review(item_id, machine, future, since))
                   if on_github else None)
        try:
            return await future
        finally:
            self.decisions.pop(item_id, None)
            if watcher:
                watcher.cancel()

    async def _watch_pr_review(self, item_id: int, machine: MachineConfig, future: asyncio.Future,
                               since: datetime) -> None:
        """Poll the crate's PR; an approving review approves the crate, "request changes" rejects it.
        Only reviews submitted after the crate arrived count, so an old review from before a
        rework doesn't decide again."""
        reported_error = False
        while not future.done():
            url = self.store.get_item(item_id).get("pr_url", "")
            if url:
                try:
                    reviews = await asyncio.to_thread(self._pr_reviews, url)
                    reported_error = False
                except Exception as exc:
                    if not reported_error:
                        await self._log(item_id, machine.id, f"[factory] could not read PR reviews: {exc}")
                        reported_error = True
                    reviews = []
                reviews = [r for r in reviews if _submitted_after(r, since)]
                changes = [r for r in reviews if r.get("state") == "CHANGES_REQUESTED"]
                approved = [r for r in reviews if r.get("state") == "APPROVED"]
                if not future.done() and (changes or approved):
                    review = (changes or approved)[-1]
                    who = (review.get("author") or {}).get("login", "someone")
                    if changes:
                        note = (review.get("body") or "").strip()
                        self.store.update_item(item_id, notes=note)
                        await self._log(item_id, machine.id, f"[factory] {who} requested changes on GitHub")
                        future.set_result("reject")
                    else:
                        await self._log(item_id, machine.id, f"[factory] {who} approved the PR on GitHub")
                        future.set_result("approve")
                    return
            await asyncio.sleep(REVIEW_POLL_SECONDS)

    def backfill_pr_states(self) -> None:
        """Crates shipped before the dock checked their PR: check them now, in the background."""
        todo = [i["id"] for i in self.store.list_items()
                if i["state"] == "shipped" and i.get("pr_url") and not i.get("pr_state")][-50:]

        async def run() -> None:
            for item_id in todo:
                await self._stamp_pr_state(item_id)

        if todo:
            self._spawn(run())

    async def _stamp_pr_state(self, item_id: int) -> None:
        """A shipping crate's PR as it really is on GitHub (merged, open or closed), so the dock
        never says merged for a PR nobody merged (one that skipped the Merger, say)."""
        url = str(self.store.get_item(item_id).get("pr_url") or "")
        if not url:
            return
        try:
            state = await asyncio.to_thread(self._pr_view_state, url)
        except Exception as exc:  # gh down: say we don't know rather than guess
            print(f"[factory] couldn't read the state of {url}: {exc}")
            state = "unknown"
        self.store.update_item(item_id, pr_state=state)
        await self.hub.emit({"type": "pr_state", "item_id": item_id, "url": url, "state": state})

    def _pr_view_state(self, url: str) -> str:
        argv = runner.resolve(["gh", "pr", "view", url, "--json", "state"], self.cfg.allowed_commands)
        r = subprocess.run(argv, cwd=self.cfg.workspace, capture_output=True, text=True, encoding="utf-8", timeout=60)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip())
        return str(json.loads(r.stdout or "{}").get("state", "") or "unknown").lower()

    def _pr_reviews(self, url: str) -> list[dict]:
        argv = runner.resolve(["gh", "pr", "view", url, "--json", "latestReviews"], self.cfg.allowed_commands)
        r = subprocess.run(argv, cwd=self.cfg.workspace, capture_output=True, text=True, encoding="utf-8", timeout=60)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip())
        return json.loads(r.stdout or "{}").get("latestReviews", []) or []

    async def _reject(self, item_id: int, machine: MachineConfig, via: str = "", into: str = "scrap") -> None:
        item = self.store.get_item(item_id)
        note = item.get("notes", "")
        await self._log(item_id, machine.id, f"[factory] rejected at {machine.name}" + (f": {note}" if note else ""))
        await self._scrap(item_id, f"{machine.name}: rejected", note, "rejected", via=via, into=into)

    async def _scrap(self, item_id: int, reason: str, output: str = "", kind: str = "work_failed",
                     via: str = "", into: str = "scrap") -> None:
        """Into a scrap bin (into: "scrap" or an extra bin's id); via: the red belt it rides
        there on (out of the fail port)."""
        self.store.update_item(item_id, scrap_kind=kind)
        await self.hub.emit({"type": "item_triaged", "item_id": item_id, "kind": kind, "reason": reason})
        await self._log(item_id, "factory", f"[factory] scrapped: {triage.KIND_WORDS.get(kind, kind)} ({reason})")
        if via:
            await self._move(item_id, via, 0.4, state="moving")
            await self._move(item_id, self._line_of(self.store.get_item(item_id)).at(into),
                             self.cfg.travel_seconds, state="scrapped")
        else:
            await self._move(item_id, self._line_of(self.store.get_item(item_id)).at(into), 0.6,
                             state="scrapped")
        tail = "\n".join(triage.explanation(output).splitlines()[-25:])[-3000:]
        await self._close_pr(item_id, kind, reason, output)
        await self._notify(item_id, "scrapped", reason + (f"\n\n{tail}" if tail else ""))

    def rollback_label(self, item: dict) -> str:
        """The label a rolled-back crate's issue or PR gets: "needs <machine>", from its outcome."""
        wanted = triage.needed_type(str(item.get("outcome", "") or ""))
        name = self.cfg.catalog[wanted].name.lower() if wanted in self.cfg.catalog else wanted
        return f"needs {name}" if name else "needs another line"

    def rollback_labels(self) -> set[str]:
        """Every label rollback_label() can give, so a resubmitted issue loses the old one."""
        return {f"needs {m.name.lower()}" for m in self.cfg.catalog.values()} | {"needs another line"}

    async def _label_for_another_line(self, item_id: int, url: str, reason: str) -> None:
        """Rolled back: the PR stays open, labelled "needs <machine>" so a storage on a line that
        has one (search label:"needs merger") picks it up."""
        label = self.rollback_label(self.store.get_item(item_id))
        name = label.removeprefix("needs ")
        m = re.search(r"github\.com/([^/]+/[^/]+)/pull/", url)
        if m:
            await self._command(item_id, "factory", ["gh", "label", "create", label, "--repo", m.group(1),
                                                     "--color", "C5DEF5", "--force"], self.cfg.workspace, quiet_fail=True)
        code = await self._command(item_id, "factory", ["gh", "pr", "edit", url, "--add-label", label],
                                   self.cfg.workspace, quiet_fail=True)
        await self._command(item_id, "factory", ["gh", "pr", "comment", url, "--body",
                                                 f"The Software Factory handed this back ({reason}). Labelled "
                                                 f"\"{label}\" so a line with a {name} can take it."],
                            self.cfg.workspace, quiet_fail=True)
        await self._log(item_id, "factory", f"[factory] labelled its PR \"{label}\" for another line" if code == 0
                        else f"[factory] couldn't label {url} \"{label}\"")

    async def _close_pr(self, item_id: int, kind: str, reason: str, output: str = "") -> None:
        """A scrapped crate's PR is no longer needed: close it (keeping its branch), unless the
        bin's close_prs rule is off, it's someone else's PR (a pr_review crate) or it's merged."""
        item = self.store.get_item(item_id)
        url = str(item.get("pr_url") or "")
        if not url or item.get("source") == "pr_review":  # never close someone else's PR
            return
        if kind == "wrong_line":
            await self._label_for_another_line(item_id, url, reason)
            return
        if not self.bin_rules(item.get("location", "")).get("close_prs"):
            await self._log(item_id, "factory", f"[factory] left its PR open (the bin doesn't close PRs): {url}")
            return
        note = str(item.get("notes", "") or "")
        if kind == "rejected":
            why = "Rejected in the Software Factory." + (f"\n\n{note}" if note else "")
        else:
            tail = "\n".join(triage.explanation(output).splitlines()[-10:])[-1500:]
            why = (f"No longer needed: the Software Factory scrapped this crate "
                   f"({triage.KIND_WORDS.get(kind, kind)}: {reason})." + (f"\n\n{note or tail}" if note or tail else ""))
        code = await self._command(item_id, "factory", ["gh", "pr", "close", url, "--comment", why],
                                   self.cfg.workspace, quiet_fail=True)
        if code == 0:
            await self.hub.emit({"type": "pr_closed", "item_id": item_id, "url": url})
        else:
            await self._log(item_id, "factory", f"[factory] couldn't close {url} (it may be merged or closed already)")

    def _scrap_rule(self, machine: MachineConfig, kind: str) -> str:
        """Why this machine may not scrap a crate for this reason, or "" if it may."""
        if not machine.scrap_bin:
            return f"{machine.name}'s red belt doesn't run to the scrap bin"
        if kind not in machine.scrap_when:
            allowed = ", ".join(triage.KIND_WORDS.get(k, k) for k in machine.scrap_when) or "nothing"
            return (f"{machine.name} only scraps for {allowed}, and this is "
                    f"{triage.KIND_WORDS.get(kind, kind)}")
        return ""

    async def _scrap_or_hold(self, item_id: int, line: Line, at: int, kind: str, reason: str,
                             output: str, machine: MachineConfig | None = None) -> str:
        """A crate that failed (or was rejected) at machine `at` and can't go back: into the
        scrap bin along the red belt if the machine's rules allow it, otherwise held at the
        machine until you decide. Returns "scrap" if it was scrapped, "retry" to run it again."""
        machine = machine or line.machines[at]
        why_not = self._scrap_rule(machine, kind)
        if not why_not:
            if kind == "rejected":
                await self._reject(item_id, machine, via=line.return_belt(at), into=machine.scrap_to)
            else:
                await self._scrap(item_id, reason, output, kind, via=line.return_belt(at), into=machine.scrap_to)
            return "scrap"
        tail = "\n".join(triage.explanation(output).splitlines()[-6:])[-600:] if output else ""
        if tail:
            await self._log(item_id, machine.id, tail)
        return await self._hold(item_id, line, at, kind, f"{triage.KIND_WORDS.get(kind, kind)} ({reason}). "
                                f"Held here: {why_not}.", output)

    async def _hold(self, item_id: int, line: Line, at: int, kind: str, why: str, output: str) -> str:
        """Keep a crate at its machine (without blocking it) until you press Run again or Scrap."""
        machine = line.machines[at]
        self.store.update_item(item_id, scrap_kind=kind, waiting_for=why[:400])
        await self._move(item_id, machine.id, 0.0, state="held")
        await self.hub.emit({"type": "item_held", "item_id": item_id, "machine": machine.id,
                             "kind": kind, "reason": why})
        await self._log(item_id, "factory", f"[factory] {why} Run it again or scrap it in the game.")
        future = asyncio.get_running_loop().create_future()
        self.decisions[item_id] = future
        try:
            while True:
                changed = asyncio.ensure_future(self._layout_changed.wait())
                try:
                    await asyncio.wait([future, changed], return_when=asyncio.FIRST_COMPLETED)
                finally:
                    changed.cancel()
                if future.done():
                    decision = future.result()
                    break
                # A new layout: if the machine now has a red belt, the crate leaves along it.
                outcome = await self._follow_new_red_belt(item_id, machine.id, kind, why, output)
                if outcome:
                    return outcome
        finally:
            if self.decisions.get(item_id) is future:
                self.decisions.pop(item_id, None)
        self.store.update_item(item_id, waiting_for="")
        if decision == "scrap":
            item = self.store.get_item(item_id)
            note = str(item.get("notes", "") or "")
            into = machine.scrap_to if machine.scrap_bin else "scrap"
            if kind == "rejected":
                await self._reject(item_id, machine, into=into)
            else:
                await self._scrap(item_id, f"{machine.name}: scrapped by you", note or output, kind, into=into)
            return "scrap"
        await self._log(item_id, machine.id, f"[factory] running {machine.name} again")
        self.store.update_item(item_id, scrap_kind="", outcome="")
        await self._move(item_id, line.belt(at), 0.3, state="waiting")
        return "retry"

    async def _follow_new_red_belt(self, item_id: int, machine_id: str, kind: str, why: str, output: str) -> str:
        """A layout was saved while a crate was held at this machine for want of a red belt. If
        the machine has one now, the crate leaves along it: back for rework, on to the next
        machine, or into the scrap bin. Returns "scrap", "handed_off" (another run of the line
        carries it on) or "" (it stays held)."""
        item = self.store.get_item(item_id)
        if self.lines.get(item.get("pipeline", "")) is None:
            return ""
        try:
            line = self._line_of(item)
        except Exception:
            return ""
        ids = [m.id for m in line.machines]
        if machine_id not in ids:
            return ""
        at = ids.index(machine_id)
        machine = line.machines[at]
        if not machine.on_fail:
            return ""
        reason = why.split(" Held here:")[0].rstrip(".")
        words = f"{triage.KIND_WORDS.get(kind, kind)} ("
        if reason.startswith(words) and reason.endswith(")"):
            reason = reason[len(words):-1]  # just the machine and step: the kind is added again
        self.decisions.pop(item_id, None)
        self.store.update_item(item_id, waiting_for="")
        await self._log(item_id, machine.id, f"[factory] the {machine.name} has a red belt now; the crate leaves along it")
        back = await self._rework_target(item_id, line, machine) if kind != "rejected" else None
        if kind == "rejected" and machine.on_fail in ids[:at]:
            back = ids.index(machine.on_fail)
        if back is not None:
            await self._send_back(item_id, line, at, back, reason, output)
            await self._ride_back(item_id, line, at, back)
            self._spawn(self._process(item_id, start=back))
            return "handed_off"
        if self._leads_on(line, at):
            line = await self._take_red_belt(item_id, line, at, kind, reason, output)
            self._spawn(self._process(item_id, start=at + 1))
            return "handed_off"
        if machine.scrap_bin and not self._scrap_rule(machine, kind):
            if kind == "rejected":
                await self._reject(item_id, machine, via=line.return_belt(at), into=machine.scrap_to)
            else:
                await self._scrap(item_id, reason, output, kind, via=line.return_belt(at), into=machine.scrap_to)
            return "scrap"
        # Its red belt runs into a bin this kind of failure may not go in: still held.
        self.store.update_item(item_id, waiting_for=why[:400])
        return ""

    def _gate_feedback(self, item_id: int, machine: MachineConfig, output: str) -> str:
        """What a crate sent back from a gate carries: your note (or a GitHub review), plus what
        the machine itself found, so the machine it goes back to knows what to fix even when
        you sent it back without a note."""
        item = self.store.get_item(item_id)
        parts = []
        note = str(item.get("notes", "") or "").strip()
        if note:
            parts.append(f"From the person who sent it back:\n{note}")
        review = str(item.get("review", "") or "").strip()
        found = (review if machine.uses_agent() else "") or output.strip()
        if found:
            parts.append(f"What the {machine.name} found:\n{found}")
        elif review:  # a gate that runs nothing (the Approver): hand on the last review instead
            parts.append(f"The last review said:\n{review}")
        if not note:
            parts.append("No note was given, so work out what to change from the findings above and "
                         "the spec. If nothing needs to change, say exactly why.")
        return "\n\n".join(parts)

    def repo_lock(self, path) -> asyncio.Lock:
        """One lock per shared repo checkout (git commands there run one at a time)."""
        locks = self.__dict__.setdefault("_repo_locks", {})
        key = str(Path(path).resolve()).lower()
        if key not in locks:
            locks[key] = asyncio.Lock()
        return locks[key]

    async def _cleanup_branch(self, item_id: int) -> None:
        """Remove a crate's old worktree and local branch so the Coder can start fresh."""
        worktree = self.cfg.worktrees / f"crate-{item_id}"
        branch = f"crate/{item_id}"
        repo = self.repo_of(self.store.get_item(item_id)).path
        async with self.repo_lock(repo):
            await self._cleanup_branch_in(item_id, worktree, branch, repo)

    async def _cleanup_branch_in(self, item_id: int, worktree: Path, branch: str, repo: Path) -> None:
        if worktree.exists():
            await self._command(item_id, "factory", ["git", "worktree", "remove", "--force", str(worktree)],
                                repo)
        if worktree.exists():
            # git can leave a half-deleted folder on Windows (read-only files in node_modules).
            await self._log(item_id, "factory", f"[factory] deleting leftover folder {worktree}")
            try:
                await asyncio.to_thread(janitor.force_remove, worktree)
            except OSError as exc:
                await self._log(item_id, "factory", f"[factory] could not delete it: {exc}")
        await self._command(item_id, "factory", ["git", "worktree", "prune"], repo)
        await self._command(item_id, "factory", ["git", "branch", "-D", branch], repo, quiet_fail=True)

    async def _command(self, item_id: int, machine: str, argv: list[str], cwd: Path, quiet_fail: bool = False) -> int:
        """Run one allowlisted command outside a machine's steps, logging its output."""
        log = lambda line: self._log(item_id, machine, line)  # noqa: E731
        try:
            resolved = runner.resolve(argv, self.cfg.allowed_commands)
        except (runner.CommandNotAllowed, FileNotFoundError) as exc:
            await log(f"[factory] refused: {exc}")
            return -1
        lines: list[str] = []

        async def collect(line: str) -> None:
            lines.append(line)

        await log("$ " + " ".join(argv))
        code = await runner.run(resolved, cwd, collect, timeout=120)
        if code == 0 or not quiet_fail:
            for line in lines:
                await log(line)
        return code

    async def _run_machine(self, item_id: int, machine: MachineConfig,
                           rework: bool = False) -> tuple[bool, str, str]:
        """Run a machine's steps, and record the run: how long it took, what Claude cost."""
        started = time.time()
        self.run_spend[item_id] = [0.0, 0]
        outcome, failed_step = "stopped", ""
        try:
            ok, failed_step, output = await self._run_steps(item_id, machine, rework)
            outcome = "ok" if ok else "failed"
            return ok, failed_step, output
        finally:
            cost, turns = self.run_spend.pop(item_id, [0.0, 0])
            item = self.store.get_item(item_id) or {}
            self.store.record_run(item_id, str(item.get("pipeline", "")), machine.id, machine.type, started,
                                  outcome, failed_step if outcome == "failed" else "", cost, turns)
            if outcome != "stopped":
                await self.hub.emit({"type": "item_cost", "item_id": item_id, **self._cost_of(item_id)})

    def _cost_of(self, item_id: int) -> dict:
        return {"cost_usd": round(self.store.item_cost(item_id), 4), "today_usd": round(self.spent_today(), 4)}

    def spent_today(self) -> float:
        return self.store.spent_since(_midnight())

    def budget(self) -> dict:
        return {"today_usd": round(self.spent_today(), 4), "daily_usd": self.cfg.daily_budget_usd,
                "crate_usd": self.cfg.crate_budget_usd}

    async def _check_budget(self, item_id: int, line: Line, at: int, machine: MachineConfig) -> str:
        """Before a Claude machine: "" to go on; "relayout" when a new layout came while the
        crate waited for tomorrow's budget; otherwise what holding it over its budget gave."""
        cap = self.cfg.crate_budget_usd
        if cap > 0 and item_id not in self.budget_ok:
            spent = self.store.item_cost(item_id)
            if spent >= cap:
                why = (f"Over its Claude budget: ${spent:.2f} of ${cap:.2f} (crate_usd under [budget] in "
                       f"factory.toml). Run it again to let it carry on anyway.")
                out = await self._hold(item_id, line, at, "blocked", why, "")
                if out == "retry":
                    self.budget_ok.add(item_id)
                return out
        daily = self.cfg.daily_budget_usd
        if daily > 0 and self.spent_today() >= daily:
            version = self.layout_version
            await self._set_waiting(item_id, machine.id, f"today's Claude budget (${daily:.2f}) is spent; it "
                                    f"carries on after midnight (daily_usd under [budget] in factory.toml)")
            if self._budget_alerted != _midnight():
                self._budget_alerted = _midnight()
                await self.hub.emit({"type": "budget_spent", "daily_usd": daily, "today_usd": round(self.spent_today(), 4)})
            while self.spent_today() >= daily:
                if self.layout_version != version:
                    await self._set_waiting(item_id, machine.id, "")
                    return "relayout"
                changed = asyncio.ensure_future(self._layout_changed.wait())
                try:
                    await asyncio.wait([changed], timeout=60)
                finally:
                    changed.cancel()
            await self._set_waiting(item_id, machine.id, "")
        return ""

    async def _run_steps(self, item_id: int, machine: MachineConfig,
                         rework: bool = False) -> tuple[bool, str, str]:
        """Run a machine's steps. Returns (ok, failed step name, the output tail of the failed
        step, or of the last step when all passed)."""
        steps = machine.steps
        await self.hub.emit({"type": "job_started", "item_id": item_id, "machine": machine.id,
                             "command": [s.name for s in steps]})
        values = self._values(self.store.get_item(item_id), machine)
        Path(values["review_dir"]).mkdir(parents=True, exist_ok=True) if any(
            "{review_dir}" in s.cwd for s in steps) else None
        index = -1
        while index + 1 < len(steps):
            index += 1
            step = steps[index]
            if rework and step.first_pass_only:
                await self._log(item_id, machine.id, f"== {machine.name} {index + 1}/{len(steps)}: "
                                f"{step.name} == (skipped: the crate is back for rework)")
                continue
            skip = _skip_reason(step.when, machine.options, values)
            if skip:
                await self._log(item_id, machine.id, f"== {machine.name} {index + 1}/{len(steps)}: "
                                f"{step.name} == (skipped: {skip})")
                continue
            await self.hub.emit({"type": "job_progress", "item_id": item_id, "machine": machine.id,
                                 "progress": index / len(steps), "step": step.name})
            code = await self._run_step(item_id, machine, index, step, values)
            if runner.stopping():
                # The backend is shutting down: not the crate's fault. It stays where it is and
                # carries on from this machine when the backend starts again.
                raise asyncio.CancelledError
            if item_id in self.wrong_machine:
                # Claude said this crate needs another kind of machine: skip the rest of the steps.
                await self._log(item_id, machine.id, f"[factory] not {machine.name} work; skipping its other steps")
                self.outputs.pop(item_id, None)
                return False, step.name, ""
            if code != 0 and is_agent_step(step) and self.health and not await self.health.check_now():
                # Claude went away mid-step: not the crate's fault. Run the step again later.
                await self._log(item_id, machine.id, f"[factory] Claude is unavailable ({self.health.reason.rstrip('.')}); "
                                f"'{step.name}' will run again when it's back")
                await self.hub.emit({"type": "job_progress", "item_id": item_id, "machine": machine.id,
                                     "progress": index / len(steps), "step": "Waiting for Claude"})
                self.outputs.pop(item_id, None)
                await self.health.wait_available()
                await self._log(item_id, machine.id, f"[factory] Claude is back; running '{step.name}' again")
                index -= 1
                continue
            if code != 0 and not is_agent_step(step):
                argv = [expand(part, values) for part in step.command]
                problem = triage.tooling_problem(argv, "\n".join(self.outputs.get(item_id, [])), code,
                                                 self.cfg.scripts_dir)
                if problem:
                    # Broken tooling, not the crate's fault: hold it here and run the step again.
                    await self._hold_for_tool(item_id, machine, index, step, problem)
                    index -= 1
                    continue
            if code != 0 and step.allow_failure:
                await self._log(item_id, machine.id, f"[factory] '{step.name}' failed but is allowed to; carrying on")
                code = 0
            if code != 0:
                await self.hub.emit({"type": "job_failed", "item_id": item_id, "machine": machine.id,
                                     "exit_code": code, "step": step.name})
                lines = self.outputs.pop(item_id, [])
                tail = "\n".join(lines[-FAILURE_TAIL_LINES:])[-FAILURE_TAIL_CHARS:]
                self.failed_at[item_id] = index
                return False, step.name, tail
        lines = self.outputs.pop(item_id, [])
        await self.hub.emit({"type": "job_progress", "item_id": item_id, "machine": machine.id, "progress": 1.0})
        await self.hub.emit({"type": "job_finished", "item_id": item_id, "machine": machine.id, "exit_code": 0})
        # The last step's output, which a gate hands on if you send the crate back.
        return True, "", "\n".join(lines[-FAILURE_TAIL_LINES:])[-FAILURE_TAIL_CHARS:]

    async def _run_step(self, item_id: int, machine: MachineConfig, index: int, step: StepConfig,
                        values: dict) -> int:
        total = len(machine.steps)
        argv = [expand(part, values) for part in step.command]
        cwd = Path(expand(step.cwd, values))
        stdin_text = expand(step.stdin, values) if step.stdin is not None else None
        if stdin_text is not None and is_agent_step(step) and machine.type in self.cfg.catalog:
            stdin_text += self._wrong_machine_hint(machine)
        self.wrong_machine.pop(item_id, None)
        fmt = formats.FORMATTERS.get(step.log_format, formats.FORMATTERS["text"])
        log = lambda line: self._log(item_id, machine.id, line)  # noqa: E731

        await log(f"== {machine.name} {index + 1}/{total}: {step.name} ==")
        await log("$ " + " ".join(argv))
        try:
            resolved = runner.resolve(argv, self.cfg.allowed_commands)
            if not cwd.is_dir():
                raise FileNotFoundError(f"working folder does not exist: {cwd}")
            repo = self.repo_of(self.store.get_item(item_id)).path
            if not (_inside(cwd, self.cfg.workspace) or _inside(cwd, repo)):
                raise runner.CommandNotAllowed(f"working folder is outside the workspace and the repo: {cwd}")
        except (runner.CommandNotAllowed, FileNotFoundError) as exc:
            await log(f"[factory] refused: {exc}")
            self.outputs[item_id] = [f"refused: {exc}"]  # for triage
            return -1

        pr_urls: list[str] = []
        output = self.outputs[item_id] = []
        # A crate that came in with its PR (one to review) keeps it: PR links in the output
        # are other PRs being talked about, not this crate's.
        item = self.store.get_item(item_id)
        # A crate that came in with its PR keeps it (a URL in the output may be another PR).
        sniff_pr = item.get("source") not in ("pr_review", "github_pr") and not item.get("head_sha")

        async def on_line(line: str) -> None:
            m = PROGRESS_RE.match(line)
            if m and step.log_format == "text":
                inner = max(0.0, min(1.0, float(m.group(1))))
                await self.hub.emit({"type": "job_progress", "item_id": item_id, "machine": machine.id,
                                     "progress": (index + inner) / total, "step": step.name})
                return
            m = triage.OUTCOME_RE.match(line.strip())
            if m:
                self.store.update_item(item_id, outcome=m.group(1))
            if line.strip() == "NO_REWORK" and step.log_format == "text":
                self.no_rework.add(item_id)
                return
            m = WAITING_RE.match(line.strip())
            if m and step.log_format == "text":
                await self._set_waiting(item_id, machine.id, (m.group(1) or "").strip()[:200])
                return
            m = RISK_RE.match(line.strip())
            if m and step.log_format == "text":
                risk, reason = m.group(1).lower(), m.group(2).strip()[:300]
                await self._stamp_risk(item_id, risk, reason)
                await log(f"[factory] stamped {risk} risk" + (f": {reason}" if reason else ""))
                return
            if sniff_pr:
                pr_urls.extend(PR_URL_RE.findall(line))
            if step.log_format == "claude-stream-json" and '"type":"result"' in line.replace(" ", ""):
                with contextlib.suppress(ValueError, TypeError, AttributeError):
                    result = json.loads(line)
                    spend = self.run_spend.setdefault(item_id, [0.0, 0])
                    spend[0] += float(result.get("total_cost_usd") or 0)
                    spend[1] += int(result.get("num_turns") or 0)
                with contextlib.suppress(ValueError, AttributeError):
                    values["agent_result"] = str(json.loads(line).get("result", "")).strip()
                    self.store.update_item(item_id, review=values["agent_result"])
                    wanted = self._wanted_type(values["agent_result"], machine)
                    if wanted:
                        self.wrong_machine[item_id] = wanted
            for out in fmt(line):
                output.append(out)
                await log(out)

        if step.cwd.strip() == "{repo}":
            # The shared checkout (fetch, worktree add): one git command at a time in it, so
            # crates running at once don't trip over each other's lock files.
            async with self.repo_lock(cwd):
                code = await runner.run(resolved, cwd, on_line, stdin_text=stdin_text, timeout=step.timeout_seconds)
        else:
            code = await runner.run(resolved, cwd, on_line, stdin_text=stdin_text, timeout=step.timeout_seconds)
        if (self.store.get_item(item_id) or {}).get("waiting_for"):
            await self._set_waiting(item_id, machine.id, "")
        if code == 0 and step.save_result_as == "pr_body" and values.get("agent_result", "").strip():
            # Claude filled in the repo's PR template: that's the body, with the issue link and
            # your attribution line under it.
            item = self.store.get_item(item_id)
            body = values["agent_result"].strip() + "\n\n" + _pr_links(item)
            values["pr_body"] = attribution.with_line(body, values.get("pr_attribution", ""))
        if code == 0 and step.save_result_as == "pr_text" and values.get("agent_result", "").strip():
            title, body = _split_pr_text(values["agent_result"])
            self.store.update_item(item_id, pr_title=title, pr_body=body)
            await log(f"[factory] PR title: {title or '(the plain one)'}")
        if code == 0 and step.save_result_as == "spec" and values.get("agent_result", "").strip():
            self.store.update_item(item_id, spec=values["agent_result"].strip())
            values["spec"] = values["agent_result"].strip()
        if code != 0:
            await log(f"[factory] step '{step.name}' failed with exit code {code}")
        elif pr_urls and pr_urls[-1] != values.get("pr_url"):
            values["pr_url"] = pr_urls[-1]
            self.store.update_item(item_id, pr_url=pr_urls[-1])
            await self.hub.emit({"type": "pr_opened", "item_id": item_id, "url": pr_urls[-1]})
            await self._notify(item_id, "pr_opened", pr_urls[-1])
        return code

    async def _set_waiting(self, item_id: int, machine_id: str, why: str) -> None:
        item = self.store.get_item(item_id) or {}
        if item.get("waiting_for", "") == why:
            return
        self.store.update_item(item_id, waiting_for=why)
        await self.hub.emit({"type": "item_waiting", "item_id": item_id, "machine": machine_id, "reason": why})
        if why:
            await self._log(item_id, "factory", f"[factory] waiting: {why}")

    async def _notify(self, item_id: int, event: str, detail: str) -> None:
        item = self.store.get_item(item_id)
        for notify in self.notifiers:
            try:
                await notify(item, event, detail)
            except Exception as exc:  # a tracker being down must not stop the line
                await self._log(item_id, "intake", f"[intake] could not update {item['source']}: {exc}")

    def _values(self, item: dict, machine: MachineConfig) -> dict:
        item_id = item["id"]
        text = item["text"]
        summary = (text.strip().splitlines() or [""])[0][:72]
        repo = self.repo_of(item)
        base = repo.base_branch or "main"
        attrib = attribution.load(repo.path)  # from Claude Code's settings, read each time
        values = {
            "id": str(item_id),
            # The commit message the Coder commits with, ending in your attribution line.
            "commit_message": attribution.with_line(f"Crate #{item_id}: {summary}", attrib["commit"]),
            "commit_attribution": attrib["commit"],
            "text": text,
            # The Refiner's version of the spec when there is one, else the spec as it came in.
            "spec": (item.get("spec") or "").strip() or text,
            "summary": summary,
            "repo": str(repo.path),
            "github_repo": repo.github,
            "worktree": str(self.cfg.worktrees / f"crate-{item_id}"),
            "branch": f"crate/{item_id}",
            "base": base,
            "base_ref": f"origin/{base}" if repo.remote else base,
            # A Document writer before the Packager writes these; otherwise the plain ones.
            "pr_title": (item.get("pr_title") or "").strip() or f"Crate #{item_id}: {summary}",
            "pr_body": attribution.with_line(
                ((item.get("pr_body") or "").strip() + "\n\n" + _pr_links(item)).strip()
                if (item.get("pr_body") or "").strip() else _pr_body(item), attrib["pr"]),
            # "yes" while the crate has its code but no PR yet: a Document writer writes the PR text.
            "pr_draft": "yes" if not item.get("pr_url") and (self.cfg.worktrees / f"crate-{item_id}").exists() else "",
            "has_worktree": "yes" if (self.cfg.worktrees / f"crate-{item_id}").exists() else "",
            "pr_attribution": attrib["pr"],
            # The repo's pull request template, if it has one (the Packager has Claude fill it in).
            "pr_template": _pr_template(self.cfg.worktrees / f"crate-{item_id}"),
            "pr_url": item.get("pr_url", ""),
            "workspace": str(self.cfg.workspace),
            "head_sha": item.get("head_sha", ""),
            "review_dir": str(self.cfg.workspace / "reviews" / f"crate-{item_id}"),
            "your_note": item.get("notes", ""),
            "notes": (f"Notes from the last attempt (address these):\n{item['notes']}"
                      if item.get("notes") else ""),
            "loops": str(item.get("loops", 0) or 0),
            "agent_result": item.get("review", ""),
            "risk": item.get("risk", "") or "",
            "risk_note": (f"The Risk analyser rated this change {item['risk']} risk"
                          + (f": {item['risk_reason']}" if item.get("risk_reason") else ".")
                          if item.get("risk") else ""),
            "python": sys.executable,
            "scripts": str(self.cfg.scripts_dir),
            "source": item.get("source", "") or "game",
            "source_ref": item.get("source_ref", "") or "",
            "issue_repo": repo.github or self.cfg.github.repo,
            "issue_label": self.cfg.github.label,
            # Where the Verifier looks up a Jira issue (the token itself stays in its variable).
            "jira_url": self.cfg.jira.base_url,
            "jira_email": self.cfg.jira.email,
            "jira_token_env": self.cfg.jira.token_env,
            "previous_plan": (f"Your previous plan was sent back. Revise it; don't start over.\n"
                              f"Why it came back:\n{item['notes']}\n\nYour previous plan:\n{item.get('review', '')}"
                              if item.get("notes") and item.get("review") else ""),
        }
        for key, value in machine.options.items():  # text settings, e.g. {reviewers}
            if isinstance(value, str):
                values.setdefault(key, value)
        values["prompt"] = expand(machine.prompt, values).strip() + "\n"
        return values


def _words(value: str) -> list[str]:
    return [w.strip() for w in re.split(r"[,\n]", value or "") if w.strip()]


def _rule_matches(kind: str, value: str, item: dict, changes: tuple[list[str], int] | None) -> bool:
    """Whether a sorter rule (graph.RULE_KINDS) matches a crate."""
    if kind == "otherwise":
        return True
    if kind == "risk":
        risk = str(item.get("risk") or "")
        if risk not in RISK_ORDER:
            return False
        want = value.rstrip("+")
        return RISK_ORDER[risk] >= RISK_ORDER.get(want, 9) if value.endswith("+") else risk == want
    if kind == "source":
        return (item.get("source") or "game") in _words(value)
    if kind == "text":
        text = f"{item.get('text', '')}\n{item.get('spec', '')}".lower()
        return any(w.lower() in text for w in _words(value))
    files, lines = changes or ([], 0)
    if kind == "size":
        return value.strip().isdigit() and lines >= int(value)
    if kind == "files":
        patterns = [w for part in _words(value) for w in part.split()]
        return any(fnmatch.fnmatch(f, pat) or f.startswith(pat.rstrip("/") + "/") for f in files for pat in patterns)
    return False


def _rule_words(kind: str, value: str) -> str:
    return {"otherwise": "everything else", "risk": f"risk {value.replace('+', ' or higher')}",
            "source": f"from {value}", "text": f"the spec mentions {value}", "files": f"changes {value}",
            "size": f"changes {value}+ lines"}.get(kind, kind)


def _skip_reason(when: str, options: dict, values: dict) -> str:
    """Why a step with this `when` is skipped, or "" if it runs. Each comma-separated term is a
    machine option (ticked) or a placeholder (not empty); "!" in front turns it round."""
    for term in [t.strip() for t in (when or "").split(",") if t.strip()]:
        negate = term.startswith("!")
        name = term.lstrip("!")
        on = bool(options[name]) if name in options else bool(str(values.get(name, "")).strip())
        if on == negate:
            words = name.replace("_", " ")
            if name in options:
                return "turned on in its settings" if negate else "turned off in its settings"
            return f"there is a {words}" if negate else f"no {words}"
    return ""


def _split_pr_text(text: str) -> tuple[str, str]:
    """A Document writer's reply: "Title: <title>" on the first line, then the description."""
    lines = text.strip().splitlines()
    title = ""
    if lines and re.match(r"^\W*title\W*:", lines[0], re.IGNORECASE):
        title = lines[0].split(":", 1)[1].strip().strip("*`# ").strip()[:200]
        lines = lines[1:]
    return title, "\n".join(lines).strip()


def _pr_body(item: dict) -> str:
    """The PR body when the repo has no PR template."""
    lines = ["Opened by the Software Factory.", "", "## Spec", "", item["text"].strip(), ""]
    links = _pr_links(item)
    return "\n".join(lines) + (links + "\n" if links else "")


def _pr_links(item: dict) -> str:
    """The line tying the PR to its issue: "Closes #n" (GitHub closes it on merge) or the Jira link."""
    if item.get("source") == "github" and item.get("source_ref"):
        # owner/name#n works from any repo (a storage can read issues from another one).
        m = re.search(r"github\.com/([^/]+/[^/]+)/issues/", str(item.get("source_url") or ""))
        return f"Closes {m.group(1)}#{item['source_ref']}" if m else f"Closes #{item['source_ref']}"
    if item.get("source") == "jira" and item.get("source_ref"):
        return f"Jira: [{item['source_ref']}]({item.get('source_url', '')})"
    return ""


# Where GitHub looks for a pull request template, in its order. A folder of templates
# (.github/PULL_REQUEST_TEMPLATE/) uses its first one by name.
PR_TEMPLATE_FILES = (".github/pull_request_template.md", "pull_request_template.md",
                     "docs/pull_request_template.md")
PR_TEMPLATE_DIRS = (".github/PULL_REQUEST_TEMPLATE", "PULL_REQUEST_TEMPLATE", "docs/PULL_REQUEST_TEMPLATE")


def _pr_template(worktree: Path) -> str:
    """The text of the repo's pull request template (any case of the file name), or ""."""
    if not worktree.is_dir():
        return ""

    def find(rel: str) -> Path | None:
        folder = worktree / Path(rel).parent
        if not folder.is_dir():
            return None
        name = Path(rel).name.lower()
        return next((p for p in sorted(folder.iterdir()) if p.is_file() and p.name.lower() == name), None)

    for rel in PR_TEMPLATE_FILES:
        found = find(rel)
        if found:
            return found.read_text(encoding="utf-8", errors="replace").strip()[:20000]
    for rel in PR_TEMPLATE_DIRS:
        parent = worktree / Path(rel).parent
        if not parent.is_dir():
            continue
        folder = next((p for p in parent.iterdir() if p.is_dir() and p.name.lower() == Path(rel).name.lower()), None)
        files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in (".md", ".txt")) if folder else []
        if files:
            return files[0].read_text(encoding="utf-8", errors="replace").strip()[:20000]
    return ""


def _clean_pipeline(p: dict) -> dict:
    """Keep only the fields a pipeline is saved with (the game sends extra drawing details)."""
    machine_keys = ("id", "type", "name", "pos", "gate", "prompt", "timeout_seconds", "auto_approve", "scrap_when",
                    "options", "rules", "retries")
    return {
        "id": p["id"], "name": p.get("name", p["id"]),
        # Saved with storages: the intake's own tick (typed specs); the rest come from its storages.
        "sources": [s for s in graph.sources_of(p) if s == "game"] if "storages" in p else graph.sources_of(p),
        **({"storages": [{"id": str(st["id"]), "source": str(st.get("source", "")), "pos": list(st["pos"]),
                          **({"query": " ".join(str(st["query"]).split())[:1000]} if str(st.get("query") or "").strip() else {}),
                          **({"repo": str(st["repo"]).strip()} if str(st.get("repo") or "").strip() else {})}
                         for st in graph.storages_of(p) if isinstance(st.get("pos"), (list, tuple))]}
           if "storages" in p else {}),
        # Stations not placed yet are left out (the pipeline is paused until they are).
        **{st: {"pos": list(p[st]["pos"]), **(_bin_extras(p[st]) if st == "scrap" else {})} for st in graph.STATIONS
           if isinstance(p.get(st), dict) and isinstance(p[st].get("pos"), (list, tuple))},
        **({"repo": graph.repo_settings(p)} if graph.repo_settings(p) else {}),
        **({"queries": graph.queries_of(p)} if graph.queries_of(p) and "storages" not in p else {}),
        **({"check_only": True} if graph.check_only(p) else {}),
        **({"max_crates": graph.max_crates(p)} if graph.max_crates(p) else {}),
        "bins": [{"id": str(b["id"]), "pos": list(b["pos"]), **_bin_extras(b)} for b in p.get("bins", [])],
        **({"docks": [{"id": str(d["id"]), "pos": list(d["pos"])} for d in p.get("docks", [])]} if p.get("docks") else {}),
        "machines": [{k: m[k] for k in machine_keys if k in m} for m in p.get("machines", [])],
        "belts": [{k: b[k] for k in ("from", "port", "to", "path", "manual", "join") if k in b} for b in p.get("belts", [])],
        **({"conveyors": graph.conveyors_of(p)} if graph.conveyors_of(p) else {}),
    }


DEFAULT_JIRA_SCRAP_STATUS = "Won't Do"


def bin_rules(raw: dict | None) -> dict:
    """What a scrap bin does to the issue of a crate that lands in it: close it on GitHub (as not
    planned) and move it to jira_status in Jira, unless close_issues is off (then it's handed
    back: unassigned, labelled with why, back to To Do). close_prs closes the crate's own PR
    (never a reviewed one), keeping its branch."""
    raw = raw if isinstance(raw, dict) else {}
    return {"close_issues": bool(raw.get("close_issues", True)),
            "close_prs": bool(raw.get("close_prs", True)),
            "jira_status": str(raw.get("jira_status") or DEFAULT_JIRA_SCRAP_STATUS).strip()[:60]}


def _bin_extras(raw: dict) -> dict:
    rules = bin_rules(raw)
    return {"close_issues": rules["close_issues"], "close_prs": rules["close_prs"],
            "jira_status": rules["jira_status"]}


def _submitted_after(review: dict, since: datetime) -> bool:
    try:
        at = datetime.fromisoformat(str(review.get("submittedAt", "")).replace("Z", "+00:00"))
    except ValueError:
        return True  # no timestamp: count it
    return at >= since


def expand(template: str, values: dict) -> str:
    """Fill {placeholders} in one pass, so text inside a spec is never expanded again."""
    return PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


async def _ignore_line(line: str) -> None:
    return None
