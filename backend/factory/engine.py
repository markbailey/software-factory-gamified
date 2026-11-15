"""The production line: intake -> belt_0 -> machine 1 -> belt_1 -> machine 2 -> ... -> dock.

A crate that fails at a machine with on_fail set goes back along the return belt to that
machine (the Coder) with the failure as its notes, keeping its branch and PR, up to
max_rework times. Any other failure goes to the scrap bin, but only from a machine with a
belt to the bin and only for the reasons it scraps for (scrap_when); otherwise the crate is
held at the machine ("held") until you run it again or scrap it. Crates only move when this
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

from . import attribution, config, formats, graph, janitor, runner, triage, workspace
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
        error, _ = graph.check(cfg.catalog, saved)
        if not error:
            return saved
        print(f"[factory] pipelines.json is not usable ({error}); using the line instead")
    entries = [{"type": m.type, "id": m.id, "name": m.name, "gate": m.gate, "prompt": m.prompt
                if m.has_prompt() else "", "on_fail": m.on_fail} for m in cfg.machines]
    pipelines = [graph.from_line(entries)]
    pipelines[0]["sources"] = ["game", "github", "jira"]
    review = graph.review_line(cfg.catalog)
    if review:
        pipelines.append(review)
    return _with_plan_line(cfg, pipelines)


def _with_plan_line(cfg: Config, pipelines: list[dict]) -> list[dict]:
    """Add the Planning line under the others if no pipeline takes PRDs yet (once: after you
    save your pipelines in the game it's part of them, and you can move or delete it)."""
    if any("prd" in graph.sources_of(p) for p in pipelines) or cfg.prd is None:
        return pipelines
    plan = graph.plan_line(cfg.catalog, pipelines)
    if plan is None or any(p.get("id") == plan["id"] for p in pipelines):
        return pipelines
    return pipelines + [plan]


class Factory:
    def __init__(self, cfg: Config, store: Store, hub: Hub):
        self.cfg = cfg
        self.store = store
        self.hub = hub
        self.layout: list[dict] = []  # every pipeline, as saved, in order
        self.problems: dict[str, str] = {}  # pipelines that can't run (paused): id -> why
        self.lines = self._use(load_pipelines(cfg))
        for pid, why in self.problems.items():
            print(f"[factory] paused: {why}")
        self.locks = self._make_locks()  # one crate per machine at a time
        self.tasks: set[asyncio.Task] = set()
        self.notifiers: list[Notifier] = []
        self.decisions: dict[int, asyncio.Future] = {}  # crates waiting at a gate
        self.outputs: dict[int, list[str]] = {}  # each crate's output from its current step
        self.no_rework: set[int] = set()  # crates whose failing step said NO_REWORK
        self.failed_at: dict[int, int] = {}  # crate -> index of the step that failed last
        self.health: AgentHealth | None = None  # set by main; None means never pause
        self.outages: dict[str, Outage] = {}  # broken tools by key (gh, git, network, ...)
        self._tool_wake = asyncio.Event()
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
            "layout": {
                "pipelines": pipelines,
                "catalog": [m.summary() for m in self.cfg.catalog.values()],
                "sources": list(graph.SOURCES),
                "belt_seconds": self.cfg.travel_seconds,
                "max_rework": self.cfg.max_rework,
                "default_repo": self.cfg.default_repo.summary(),
            },
            "items": self.store.list_items(),
            "agent": self.health.status() if self.health else {"available": True, "enabled": False},
            "tools": self.tool_status(),
        }

    def line_for_source(self, source: str) -> Line | None:
        """The pipeline a new crate from this source goes to. Typed specs, GitHub issues and
        Jira issues fall back to the "game" pipeline (or the first) if none takes them."""
        for line in self.lines.values():
            if source in line.sources:
                return line
        if source in ("game", "github", "jira"):
            for line in self.lines.values():
                if "game" in line.sources:
                    return line
            return next(iter(self.lines.values()), None)
        return None

    async def submit(self, text: str, source: str = "game", source_ref: str = "", source_url: str = "",
                     pr_url: str = "", head_sha: str = "", pipeline: str = "") -> dict | None:
        """Put a new crate on a pipeline's intake. Returns the item, or None if no pipeline
        takes crates from this source."""
        line = self.lines.get(pipeline) if pipeline else self.line_for_source(source)
        if line is None:
            return None
        item = self.store.create_item(text, source, source_ref, source_url, pipeline=line.id,
                                      location=line.at("intake"), pr_url=pr_url, head_sha=head_sha)
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
        error, _ = graph.check(self.cfg.catalog, pipelines)
        if error:
            return error
        busy = self.busy()
        if busy:
            ids = ", ".join(f"#{i}" for i in busy[:5])
            return f"wait until the factory is empty before changing it (still working on {ids})"
        clean = [_clean_pipeline(p) for p in pipelines]
        old = (self.layout, self.problems)
        lines = self._use(clean)
        error = await self.prepare_repos(lines.values())
        if error:
            self.layout, self.problems = old
            return error
        self.lines = lines
        self.locks = self._make_locks()
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
        """Clone or fetch each line's repo, filling in its base branch if it wasn't set.
        Returns what went wrong, or None."""
        for line in lines:
            try:
                notes = await asyncio.to_thread(workspace.ensure_repo, self.cfg, line.repo)
            except Exception as exc:
                return f"{line.name}: couldn't get its repo ready ({exc})"
            for note in notes:
                print(f"[factory] {line.name}: {note}")
        return None

    def repos(self) -> list[RepoConfig]:
        """Every repo the lines work on (the default one first), each once."""
        out = {self.cfg.default_repo.path: self.cfg.default_repo}
        for line in self.lines.values():
            out.setdefault(line.repo.path, line.repo)
        return list(out.values())

    def repo_of(self, item: dict) -> RepoConfig:
        return self._line_of(item).repo

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
        if item["state"] == "held":
            return await self.decide(item_id, "retry", note)
        if item["state"] != "scrapped":
            return f"crate #{item_id} is not in the scrap bin"
        line = self.lines.get(item.get("pipeline", ""))
        if line is None:
            return (f"crate #{item_id}'s pipeline is paused or gone: fix it in build mode, then run it again"
                    if self.lines else "the factory has no running pipelines")
        self.store.update_item(item_id, notes=note, review="", state="queued", loops=0, route="",
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
    async def _move(self, item_id: int, to: str, duration: float = 0.0, state: str | None = None,
                    wait: bool = True) -> None:
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
                       resume_hold: bool = False) -> None:
        travel = self.cfg.travel_seconds
        line = self._line_of(self.store.get_item(item_id))
        machines = line.machines
        belt = line.belt
        if start == 0 and not resume_gate:
            await self._move(item_id, belt(0), travel, state="waiting")

        i = start
        rework = False  # True while the crate is back for rework (skips first_pass_only steps)
        if resume_hold:
            item = self.store.get_item(item_id)
            if await self._hold(item_id, line, i, str(item.get("scrap_kind") or "work_failed"),
                                str(item.get("waiting_for") or "held after a restart"), "") == "scrap":
                return
            rework = True
        while i < len(machines):
            machine = machines[i]
            if machine.sorter:
                # Straight through: it picks an output, and the crate carries on along it.
                await self._move(item_id, machine.id, 0.3, state="running")
                port, why = await self._sort(item_id, machine)
                if port is None:
                    if await self._hold(item_id, line, i, "blocked", f"{machine.name}: {why}.", "") == "scrap":
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
                    await self._wait_for_agent(item_id, machine)
                await self._wait_for_tools(item_id, machine)
                lock = contextlib.nullcontext() if machine.parallel else self.locks[machine.id]
                async with lock:
                    await self._move(item_id, machine.id, 0.3, state="running")
                    ok, failed_step, output = await self._run_machine(item_id, machine, rework)
                    gate_output = output
                    if not ok:
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
                if failed is not None:
                    # Out of the machine (so the next crate can use it): scrapped, or held here.
                    if await self._scrap_or_hold(item_id, line, i, *failed) == "scrap":
                        return
                    # You pressed Run again: steps that already ran once (Make branch) are skipped.
                    rework = rework or any(st.first_pass_only for st in machine.steps[:self.failed_at.get(item_id, 0)])
                    continue
                if back_to is not None:
                    await self._ride_back(item_id, line, i, back_to)
                    i, rework = back_to, True
                    continue
                if not machine.gate:
                    await asyncio.sleep(travel)
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
                if await self._scrap_or_hold(item_id, line, i, "rejected", f"{machine.name}: rejected",
                                             note, machine=machine) == "scrap":
                    return
                rework, resume_gate = True, False
                continue
            await self._send_back(item_id, line, i, back_to, "sent back by you",
                                  self._gate_feedback(item_id, machine, gate_output))
            await self._ride_back(item_id, line, i, back_to)
            i, resume_gate, rework = back_to, False, True

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
        target = machine.on_fail if machine.on_fail in ids else (machine.id if machine.rework_here else "")
        if not target:
            return None
        loops = int(self.store.get_item(item_id).get("loops", 0) or 0)
        if loops >= self.cfg.max_rework:
            await self._log(item_id, machine.id,
                            f"[factory] already sent back {loops} times (max_rework); scrapping instead")
            return None
        return ids.index(target)

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
        """Whether a gate lets the crate through by itself: its steps passed (so the review
        looks good) and the stamped risk is no higher than the machine's setting."""
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
        await self._log(item_id, machine.id, f"[factory] review looks good and the risk is {risk}: "
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
        if item.get("pr_url") and item.get("source") != "pr_review":  # never close someone else's PR
            comment = "Rejected in the Software Factory." + (f"\n\n{note}" if note else "")
            await self._command(item_id, machine.id, ["gh", "pr", "close", item["pr_url"], "--comment", comment],
                                self.cfg.workspace)
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
            await self._move(item_id, self._line_of(self.store.get_item(item_id)).at("scrap"), 0.6,
                             state="scrapped")
        tail = "\n".join(triage.explanation(output).splitlines()[-25:])[-3000:]
        await self._notify(item_id, "scrapped", reason + (f"\n\n{tail}" if tail else ""))

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
            decision = await future
        finally:
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

    def _gate_feedback(self, item_id: int, machine: MachineConfig, output: str) -> str:
        """What a crate sent back from a gate carries: your note (or a GitHub review), plus what
        the machine itself found, so the machine it goes back to knows what to fix even when
        you sent it back without a note."""
        item = self.store.get_item(item_id)
        parts = []
        note = str(item.get("notes", "") or "").strip()
        if note:
            parts.append(f"From the person who sent it back:\n{note}")
        found = str(item.get("review", "") or "").strip() if machine.uses_agent() else ""
        if not found:
            found = output.strip()
        if found:
            parts.append(f"What the {machine.name} found:\n{found}")
        if not note:
            parts.append("No note was given, so work out what to change from the findings above and "
                         "the spec. If nothing needs to change, say exactly why.")
        return "\n\n".join(parts)

    async def _cleanup_branch(self, item_id: int) -> None:
        """Remove a crate's old worktree and local branch so the Coder can start fresh."""
        worktree = self.cfg.worktrees / f"crate-{item_id}"
        branch = f"crate/{item_id}"
        repo = self.repo_of(self.store.get_item(item_id)).path
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
        sniff_pr = item.get("source") != "pr_review" and not item.get("head_sha")

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
                with contextlib.suppress(ValueError):
                    values["agent_result"] = str(json.loads(line).get("result", "")).strip()
                    self.store.update_item(item_id, review=values["agent_result"])
            for out in fmt(line):
                output.append(out)
                await log(out)

        code = await runner.run(resolved, cwd, on_line, stdin_text=stdin_text, timeout=step.timeout_seconds)
        if (self.store.get_item(item_id) or {}).get("waiting_for"):
            await self._set_waiting(item_id, machine.id, "")
        if code == 0 and step.save_result_as == "pr_body" and values.get("agent_result", "").strip():
            # Claude filled in the repo's PR template: that's the body, with the issue link and
            # your attribution line under it.
            item = self.store.get_item(item_id)
            body = values["agent_result"].strip() + "\n\n" + _pr_links(item)
            values["pr_body"] = attribution.with_line(body, values.get("pr_attribution", ""))
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
            "pr_title": f"Crate #{item_id}: {summary}",
            "pr_body": attribution.with_line(_pr_body(item), attrib["pr"]),
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


def _pr_body(item: dict) -> str:
    """The PR body when the repo has no PR template."""
    lines = ["Opened by the Software Factory.", "", "## Spec", "", item["text"].strip(), ""]
    links = _pr_links(item)
    return "\n".join(lines) + (links + "\n" if links else "")


def _pr_links(item: dict) -> str:
    """The line tying the PR to its issue: "Closes #n" (GitHub closes it on merge) or the Jira link."""
    if item.get("source") == "github" and item.get("source_ref"):
        return f"Closes #{item['source_ref']}"
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
                    "options", "rules")
    return {
        "id": p["id"], "name": p.get("name", p["id"]), "sources": graph.sources_of(p),
        # Stations not placed yet are left out (the pipeline is paused until they are).
        **{st: {"pos": list(p[st]["pos"])} for st in graph.STATIONS
           if isinstance(p.get(st), dict) and isinstance(p[st].get("pos"), (list, tuple))},
        **({"repo": graph.repo_settings(p)} if graph.repo_settings(p) else {}),
        "bins": [{"id": str(b["id"]), "pos": list(b["pos"])} for b in p.get("bins", [])],
        "machines": [{k: m[k] for k in machine_keys if k in m} for m in p.get("machines", [])],
        "belts": [{k: b[k] for k in ("from", "port", "to", "path") if k in b} for b in p.get("belts", [])],
    }


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
