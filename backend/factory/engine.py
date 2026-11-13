"""The production line: intake -> belt_0 -> machine 1 -> belt_1 -> machine 2 -> ... -> dock.

A crate that fails at any machine goes to the scrap bin. Crates only move when this
code moves them, and every move is an event the game renders.
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Awaitable, Callable

from . import formats, runner
from .config import Config, MachineConfig, StepConfig
from .hub import Hub
from .store import Store

PROGRESS_RE = re.compile(r"^PROGRESS\s+([0-9]*\.?[0-9]+)\s*$")
PR_URL_RE = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+")
PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

# Called when a crate reaches a milestone: (item, event, detail). event is "pr_opened" or "scrapped".
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
                "machines": [
                    {"id": m.id, "name": m.name, "steps": [s.name for s in m.steps]} for m in machines
                ],
                "belt_seconds": self.cfg.travel_seconds,
            },
            "items": self.store.list_items(),
        }

    async def submit(self, text: str, source: str = "game", source_ref: str = "", source_url: str = "") -> dict:
        item = self.store.create_item(text, source, source_ref, source_url)
        await self.hub.emit({"type": "item_created", "item": item})
        task = asyncio.create_task(self._process(item["id"]))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return item

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

    async def _process(self, item_id: int) -> None:
        travel = self.cfg.travel_seconds
        machines = self.cfg.machines
        await self._move(item_id, belt(0), travel, state="waiting")

        for i, machine in enumerate(machines):
            async with self.locks[machine.id]:
                await self._move(item_id, machine.id, 0.3, state="running")
                ok, failed_step = await self._run_machine(item_id, machine)
                if not ok:
                    await self._move(item_id, "scrap", 0.6, state="scrapped")
                    await self._notify(item_id, "scrapped", f"{machine.name}: {failed_step}")
                    return
                # Leave the machine before releasing it, so the next crate can enter.
                last = i == len(machines) - 1
                await self._move(item_id, belt(i + 1), travel, state="moving" if last else "waiting", wait=False)
            await asyncio.sleep(travel)

        await self._move(item_id, "dock", 0.3, state="shipped")

    async def _run_machine(self, item_id: int, machine: MachineConfig) -> tuple[bool, str]:
        steps = machine.steps
        await self.hub.emit({"type": "job_started", "item_id": item_id, "machine": machine.id,
                             "command": [s.name for s in steps]})
        values = self._values(self.store.get_item(item_id), machine)
        for index, step in enumerate(steps):
            await self.hub.emit({"type": "job_progress", "item_id": item_id, "machine": machine.id,
                                 "progress": index / len(steps), "step": step.name})
            code = await self._run_step(item_id, machine, index, step, values)
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
            for out in fmt(line):
                await log(out)

        code = await runner.run(resolved, cwd, on_line, stdin_text=stdin_text, timeout=step.timeout_seconds)
        if code != 0:
            await log(f"[factory] step '{step.name}' failed with exit code {code}")
        elif pr_urls:
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
