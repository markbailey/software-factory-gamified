"""The production line: intake -> belt_0 -> machine 1 -> belt_1 -> machine 2 -> ... -> dock.

A crate that fails at any machine goes to the scrap bin. Crates only move when this
code moves them, and every move is an event the game renders.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Awaitable, Callable

from . import config, formats, janitor, runner
from .config import Config, MachineConfig, StepConfig
from .hub import Hub
from .store import Store

PROGRESS_RE = re.compile(r"^PROGRESS\s+([0-9]*\.?[0-9]+)\s*$")
PR_URL_RE = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+")
PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
REVIEW_POLL_SECONDS = 20

# Called when a crate reaches a milestone: (item, event, detail).
# event is "pr_opened", "awaiting_approval", "scrapped" or "shipped".
Notifier = Callable[[dict, str, str], Awaitable[None]]


def belt(i: int) -> str:
    """Location name of the belt in front of machine i (belt_n is after the last machine)."""
    return f"belt_{i}"


class Factory:
    def __init__(self, cfg: Config, store: Store, hub: Hub):
        self.cfg = cfg
        self.store = store
        self.hub = hub
        self.locks = {m.id: asyncio.Lock() for m in cfg.machines}  # one crate per machine at a time
        self.tasks: set[asyncio.Task] = set()
        self.notifiers: list[Notifier] = []
        self.decisions: dict[int, asyncio.Future] = {}  # crates waiting at a gate

    # Public ----------------------------------------------------------------
    def snapshot(self) -> dict:
        machines = self.cfg.machines
        locations = ["intake"]
        for i, m in enumerate(machines):
            locations += [belt(i), m.id]
        locations += [belt(len(machines)), "dock", "scrap"]
        return {
            "type": "snapshot",
            "seq": self.store.last_seq(),
            "layout": {
                "locations": locations,
                "machines": [m.summary() for m in machines],
                "catalog": [m.summary() for m in self.cfg.catalog.values()],
                "belt_seconds": self.cfg.travel_seconds,
            },
            "items": self.store.list_items(),
        }

    async def submit(self, text: str, source: str = "game", source_ref: str = "", source_url: str = "") -> dict:
        item = self.store.create_item(text, source, source_ref, source_url)
        await self.hub.emit({"type": "item_created", "item": item})
        self._spawn(self._process(item["id"]))
        return item

    def busy(self) -> list[int]:
        """Crates still somewhere in the line (including waiting for you)."""
        return [i["id"] for i in self.store.list_items() if i["state"] not in ("shipped", "scrapped")]

    async def set_line(self, entries: list) -> str | None:
        """Replace the line with one built in the game. Returns an error message, or None."""
        error = config.validate_line(self.cfg.catalog, entries)
        if error:
            return error
        busy = self.busy()
        if busy:
            ids = ", ".join(f"#{i}" for i in busy[:5])
            return f"wait until the line is empty before changing it (still working on {ids})"
        clean = [{k: e[k] for k in ("type", "id", "name", "gate", "prompt") if k in e} for e in entries]
        self.cfg.machines = config.build_line(self.cfg.catalog, clean)
        self.locks = {m.id: asyncio.Lock() for m in self.cfg.machines}
        config.write_line(clean)
        await self.hub.broadcast(self.snapshot())
        return None

    def resume_waiting(self) -> int:
        """After a restart, put crates that were waiting at a gate back in front of it."""
        index = {m.id: i for i, m in enumerate(self.cfg.machines)}
        count = 0
        for item in self.store.list_items():
            if item["state"] == "awaiting_approval" and item["location"] in index:
                self._spawn(self._process(item["id"], start=index[item["location"]], resume_gate=True))
                count += 1
        return count

    async def decide(self, item_id: int, decision: str, note: str = "") -> str | None:
        """Approve or reject a crate waiting at a gate. Returns an error message, or None."""
        future = self.decisions.get(item_id)
        if future is None or future.done():
            return f"crate #{item_id} is not waiting for approval"
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
        if item["state"] != "scrapped":
            return f"crate #{item_id} is not in the scrap bin"
        self.store.update_item(item_id, notes=note, review="", state="queued")
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

    async def _process(self, item_id: int, start: int = 0, resume_gate: bool = False) -> None:
        travel = self.cfg.travel_seconds
        machines = self.cfg.machines
        if start == 0 and not resume_gate:
            await self._move(item_id, belt(0), travel, state="waiting")

        for i in range(start, len(machines)):
            machine = machines[i]
            last = i == len(machines) - 1
            next_state = "moving" if last else "waiting"
            if not (resume_gate and i == start):
                lock = contextlib.nullcontext() if machine.parallel else self.locks[machine.id]
                async with lock:
                    await self._move(item_id, machine.id, 0.3, state="running")
                    ok, failed_step = await self._run_machine(item_id, machine)
                    if not ok:
                        await self._scrap(item_id, f"{machine.name}: {failed_step}")
                        return
                    if not machine.gate:
                        # Leave the machine before releasing it, so the next crate can enter.
                        await self._move(item_id, belt(i + 1), travel, state=next_state, wait=False)
                if not machine.gate:
                    await asyncio.sleep(travel)
                    continue

            # Gate: the crate waits here (without blocking the machine) until you decide.
            decision = await self._await_decision(item_id, machine)
            if decision != "approve":
                await self._reject(item_id, machine)
                return
            await self._log(item_id, machine.id, f"[factory] approved at {machine.name}")
            await self._move(item_id, belt(i + 1), travel, state=next_state)

        await self._move(item_id, "dock", 0.3, state="shipped")
        await self._notify(item_id, "shipped", "")

    async def _await_decision(self, item_id: int, machine: MachineConfig) -> str:
        future = asyncio.get_running_loop().create_future()
        self.decisions[item_id] = future
        await self._move(item_id, machine.id, 0.0, state="awaiting_approval")
        where = "in the game" + (" or with a review on the PR" if machine.github_review else "")
        await self._log(item_id, machine.id, f"[factory] waiting at {machine.name} for you to approve or reject {where}")
        await self._notify(item_id, "awaiting_approval", machine.name)
        watcher = asyncio.create_task(self._watch_pr_review(item_id, machine, future)) if machine.github_review else None
        try:
            return await future
        finally:
            self.decisions.pop(item_id, None)
            if watcher:
                watcher.cancel()

    async def _watch_pr_review(self, item_id: int, machine: MachineConfig, future: asyncio.Future) -> None:
        """Poll the crate's PR; an approving review approves the crate, "request changes" rejects it."""
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
        r = subprocess.run(argv, cwd=self.cfg.repo, capture_output=True, text=True, encoding="utf-8", timeout=60)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip())
        return json.loads(r.stdout or "{}").get("latestReviews", []) or []

    async def _reject(self, item_id: int, machine: MachineConfig) -> None:
        item = self.store.get_item(item_id)
        note = item.get("notes", "")
        await self._log(item_id, machine.id, f"[factory] rejected at {machine.name}" + (f": {note}" if note else ""))
        if item.get("pr_url"):
            comment = "Rejected in the Software Factory." + (f"\n\n{note}" if note else "")
            await self._command(item_id, machine.id, ["gh", "pr", "close", item["pr_url"], "--comment", comment],
                                self.cfg.repo)
        await self._scrap(item_id, f"{machine.name}: rejected")

    async def _scrap(self, item_id: int, reason: str) -> None:
        await self._move(item_id, "scrap", 0.6, state="scrapped")
        await self._notify(item_id, "scrapped", reason)

    async def _cleanup_branch(self, item_id: int) -> None:
        """Remove a crate's old worktree and local branch so the Coder can start fresh."""
        worktree = self.cfg.worktrees / f"crate-{item_id}"
        branch = f"crate/{item_id}"
        if worktree.exists():
            await self._command(item_id, "factory", ["git", "worktree", "remove", "--force", str(worktree)],
                                self.cfg.repo)
        if worktree.exists():
            # git can leave a half-deleted folder on Windows (read-only files in node_modules).
            await self._log(item_id, "factory", f"[factory] deleting leftover folder {worktree}")
            try:
                await asyncio.to_thread(janitor.force_remove, worktree)
            except OSError as exc:
                await self._log(item_id, "factory", f"[factory] could not delete it: {exc}")
        await self._command(item_id, "factory", ["git", "worktree", "prune"], self.cfg.repo)
        await self._command(item_id, "factory", ["git", "branch", "-D", branch], self.cfg.repo, quiet_fail=True)

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

    async def _run_machine(self, item_id: int, machine: MachineConfig) -> tuple[bool, str]:
        steps = machine.steps
        await self.hub.emit({"type": "job_started", "item_id": item_id, "machine": machine.id,
                             "command": [s.name for s in steps]})
        values = self._values(self.store.get_item(item_id), machine)
        for index, step in enumerate(steps):
            await self.hub.emit({"type": "job_progress", "item_id": item_id, "machine": machine.id,
                                 "progress": index / len(steps), "step": step.name})
            code = await self._run_step(item_id, machine, index, step, values)
            if code != 0 and step.allow_failure:
                await self._log(item_id, machine.id, f"[factory] '{step.name}' failed but is allowed to; carrying on")
                code = 0
            if code != 0:
                await self.hub.emit({"type": "job_failed", "item_id": item_id, "machine": machine.id,
                                     "exit_code": code, "step": step.name})
                return False, step.name
        await self.hub.emit({"type": "job_progress", "item_id": item_id, "machine": machine.id, "progress": 1.0})
        await self.hub.emit({"type": "job_finished", "item_id": item_id, "machine": machine.id, "exit_code": 0})
        return True, ""

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
            if not _inside(cwd, self.cfg.workspace):
                raise runner.CommandNotAllowed(f"working folder is outside the workspace: {cwd}")
        except (runner.CommandNotAllowed, FileNotFoundError) as exc:
            await log(f"[factory] refused: {exc}")
            return -1

        pr_urls: list[str] = []

        async def on_line(line: str) -> None:
            m = PROGRESS_RE.match(line)
            if m and step.log_format == "text":
                inner = max(0.0, min(1.0, float(m.group(1))))
                await self.hub.emit({"type": "job_progress", "item_id": item_id, "machine": machine.id,
                                     "progress": (index + inner) / total, "step": step.name})
                return
            pr_urls.extend(PR_URL_RE.findall(line))
            if step.log_format == "claude-stream-json" and '"type":"result"' in line.replace(" ", ""):
                with contextlib.suppress(ValueError):
                    values["agent_result"] = str(json.loads(line).get("result", "")).strip()
                    self.store.update_item(item_id, review=values["agent_result"])
            for out in fmt(line):
                await log(out)

        code = await runner.run(resolved, cwd, on_line, stdin_text=stdin_text, timeout=step.timeout_seconds)
        if code != 0:
            await log(f"[factory] step '{step.name}' failed with exit code {code}")
        elif pr_urls and pr_urls[-1] != values.get("pr_url"):
            values["pr_url"] = pr_urls[-1]
            self.store.update_item(item_id, pr_url=pr_urls[-1])
            await self.hub.emit({"type": "pr_opened", "item_id": item_id, "url": pr_urls[-1]})
            await self._notify(item_id, "pr_opened", pr_urls[-1])
        return code

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
        base = self.cfg.base_branch
        values = {
            "id": str(item_id),
            "text": text,
            "summary": summary,
            "repo": str(self.cfg.repo),
            "worktree": str(self.cfg.worktrees / f"crate-{item_id}"),
            "branch": f"crate/{item_id}",
            "base": base,
            "base_ref": f"origin/{base}" if self.cfg.remote else base,
            "pr_title": f"Crate #{item_id}: {summary}",
            "pr_body": _pr_body(item),
            "pr_url": item.get("pr_url", ""),
            "notes": (f"Notes from the last attempt (address these):\n{item['notes']}"
                      if item.get("notes") else ""),
            "agent_result": item.get("review", ""),
            "python": sys.executable,
            "scripts": str(self.cfg.scripts_dir),
        }
        values["prompt"] = expand(machine.prompt, values).strip() + "\n"
        return values


def _pr_body(item: dict) -> str:
    lines = ["Opened by the Software Factory.", "", "## Spec", "", item["text"].strip(), ""]
    if item.get("source") == "github" and item.get("source_ref"):
        lines += [f"Closes #{item['source_ref']}", ""]
    elif item.get("source") == "jira" and item.get("source_ref"):
        lines += [f"Jira: [{item['source_ref']}]({item.get('source_url', '')})", ""]
    return "\n".join(lines)


def expand(template: str, values: dict) -> str:
    """Fill {placeholders} in one pass, so text inside a spec is never expanded again."""
    return PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), template)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
