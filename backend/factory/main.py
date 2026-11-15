"""FastAPI app: one WebSocket that the Godot game connects to."""
from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

import asyncio
import dataclasses

from . import config, graph, health, intake, janitor, workspace
from .engine import Factory
from .hub import Hub
from .store import Store

cfg = config.load()
cfg.workspace.mkdir(parents=True, exist_ok=True)
store = Store(cfg.db_path)
hub = Hub(store)
factory = Factory(cfg, store, hub)
factory.health = health.AgentHealth(health.HealthConfig.from_toml(), cfg.allowed_commands, cfg.workspace,
                                    hub.broadcast)

MAX_SPEC_CHARS = 20000  # a pasted PRD can be long
# Intake pollers, for the game's intake window: "jira" and "pr_review" by source; GitHub issues
# and PRDs per pipeline, from that pipeline's repo ("github:<pipeline id>", "prd:<pipeline id>").
feeds: dict[str, intake.Feed] = {}
feed_tasks: dict[str, asyncio.Task] = {}
REPO_SOURCES = ("github", "prd")


def feed_for(source: str, pipeline: str) -> intake.Feed | None:
    """The poller a pipeline's intake window shows for this source."""
    if source in REPO_SOURCES:
        line = factory.lines.get(pipeline)
        if line is not None and source in line.sources:
            return feeds.get(f"{source}:{pipeline}")
        owner = factory.line_for_source(source)
        return feeds.get(f"{source}:{owner.id}") if owner is line and owner is not None else None
    return feeds.get(source)


def _start_feed(key: str, adapter, seconds: float) -> None:
    factory.notifiers.append(adapter.notify)
    feed = feeds[key] = intake.Feed(adapter, seconds)
    feed_tasks[key] = asyncio.create_task(feed.run())


def _stop_feed(key: str) -> None:
    feed = feeds.pop(key, None)
    task = feed_tasks.pop(key, None)
    if task:
        task.cancel()
    if feed and feed.adapter.notify in factory.notifiers:
        factory.notifiers.remove(feed.adapter.notify)


def sync_feeds() -> None:
    """Watch the issues (and PRDs) of each pipeline's repo: start pollers for pipelines that
    take them and stop ones whose pipeline or repo changed. Run at startup and after a save."""
    wanted: dict[str, tuple] = {}
    for line in factory.lines.values():
        repo = line.repo
        for source, base in (("github", cfg.github if cfg.github.enabled else None), ("prd", cfg.prd)):
            # A pipeline takes GitHub issues if its intake says so, or (for "github") if it's the one
            # they fall back to.
            if base is None or not repo.github:
                continue
            if source not in line.sources and factory.line_for_source(source) is not line:
                continue
            wanted[f"{source}:{line.id}"] = (source, line, dataclasses.replace(base, repo=repo.github))
    for key in list(feeds):
        if key.split(":")[0] in REPO_SOURCES:
            have = feeds[key].adapter
            want = wanted.get(key)
            if want is None or have.cfg.repo != want[2].repo:
                _stop_feed(key)
    for key, (source, line, settings) in wanted.items():
        if key in feeds:
            continue
        kind = intake.GitHubIntake if source == "github" else intake.PrdIntake
        adapter = kind(settings, factory, pipeline=line.id,
                       default=settings.repo.lower() == cfg.default_repo.github.lower())
        _start_feed(key, adapter, settings.poll_seconds)
        what = f"labelled '{settings.label}'" + (" (PRDs to plan)" if source == "prd" else "")
        print(f"[intake] {line.name}: watching GitHub issues in {settings.repo} {what}")
SOURCE_SECTIONS = {"github": "intake.github", "jira": "intake.jira", "pr_review": "intake.review_requests",
                   "prd": "intake.prd"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    interrupted = store.scrap_interrupted()
    if interrupted:
        print(f"[factory] scrapped {len(interrupted)} crate(s) left in flight by the last run")
    try:
        for note in workspace.ensure_repo(cfg):
            print(f"[factory] {note}")
    except Exception as exc:
        print(f"[factory] could not prepare the test project repo: {exc}")
    print(f"[factory] test project: {cfg.repo}")
    for line in factory.lines.values():
        if line.repo.path == cfg.default_repo.path and line.repo.base_branch == cfg.base_branch:
            continue
        error = await factory.prepare_repos([line])
        print(f"[factory] {error}" if error else f"[factory] {line.name} works on {line.repo.github or line.repo.path}"
              f" ({line.repo.base_branch})")
    pollers = []
    adapters = []
    sync_feeds()
    if cfg.jira.enabled:
        adapters.append((intake.JiraIntake(cfg.jira, factory), cfg.jira.poll_seconds))
        print(f"[intake] watching Jira: {cfg.jira.jql}")
    reviews = intake.ReviewIntakeConfig.from_toml()
    if reviews.enabled:
        adapters.append((intake.ReviewRequestIntake(reviews, factory), reviews.poll_seconds))
        print(f"[intake] watching pull requests waiting for review by {reviews.reviewer}")
    pollers.append(asyncio.create_task(factory.health.run()))
    if factory.health.cfg.enabled:
        print("[health] checking that Claude is available every "
              f"{factory.health.cfg.interval_seconds:.0f}s")
    pollers.append(asyncio.create_task(
        janitor.run(cfg, store, cfg.cleanup_interval_minutes, cfg.keep_scrapped_hours, factory.repos)))
    for adapter, seconds in adapters:
        _start_feed(adapter.source, adapter, seconds)
    waiting = factory.resume_waiting()
    if waiting:
        print(f"[factory] {waiting} crate(s) still waiting for your approval")
    print(f"[factory] game should connect to ws://{cfg.host}:{cfg.port}/ws")
    yield
    for task in pollers + list(feed_tasks.values()):
        task.cancel()


app = FastAPI(title="Software Factory", lifespan=lifespan)


async def intake_listing(pipeline: str) -> dict:
    """What each of a pipeline's intake sources has: the issues or PRs out there now, and
    which of them are already crates."""
    line = factory.lines.get(pipeline)
    out = {"type": "intake_list", "pipeline": pipeline, "sources": []}
    if line is None:
        return out
    out["repo"] = line.repo.summary()
    wanted = [s for s in ("game", "github", "jira", "pr_review", "prd")
              if s in line.sources or feed_for(s, pipeline) is not None]
    jobs = []
    for source in wanted:
        feed = feed_for(source, pipeline)
        if source == "game":
            jobs.append(asyncio.sleep(0, {"source": "game", "enabled": True, "items": [],
                                          "about": "Specs you type in the box at the top of the game."}))
        elif feed is not None:
            jobs.append(feed.listing())
        elif source in REPO_SOURCES and (cfg.github.enabled if source == "github" else cfg.prd is not None) \
                and not line.repo.github:
            jobs.append(asyncio.sleep(0, {"source": source, "enabled": False, "items": [], "about":
                "This pipeline's repo isn't on GitHub, so there are no issues to watch. Set its GitHub repo "
                "in the intake's settings."}))
        else:
            jobs.append(asyncio.sleep(0, {"source": source, "enabled": False, "items": [], "about":
                f"Turned off: set enabled = true under [{SOURCE_SECTIONS.get(source, 'intake')}] "
                f"in backend/factory.toml and restart the backend."}))
    out["sources"] = list(await asyncio.gather(*jobs))
    return out


async def intake_command(ws: WebSocket, msg: dict) -> None:
    """intake_list / intake_pull / intake_check, run beside the socket loop (they call gh)."""
    kind = msg.get("type")
    pipeline = str(msg.get("pipeline", ""))
    source = str(msg.get("source", ""))
    try:
        if kind == "intake_pull":
            feed = feed_for(source, pipeline)
            if feed is None:
                raise RuntimeError(f"{source or 'that source'} isn't switched on")
            item = await feed.pull(str(msg.get("ref", "")))
            if item is None:
                raise RuntimeError("no pipeline takes crates from there")
        elif kind == "intake_check":
            sources = [source] if source else ["github", "jira", "pr_review", "prd"]
            targets = [f for f in (feed_for(s, pipeline) for s in sources) if f is not None]
            await asyncio.gather(*(f.poll_now() for f in targets))
        if pipeline:
            await ws.send_json(await intake_listing(pipeline))
    except Exception as exc:
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "error", "message": str(exc)})
            if pipeline:
                await ws.send_json(await intake_listing(pipeline))


async def repo_check(ws: WebSocket, msg: dict) -> None:
    """The intake settings' Check button: does this repo exist, can you push to it, where will it
    be cloned. Answers with a repo_check message."""
    raw = msg.get("repo") if isinstance(msg.get("repo"), dict) else {}
    pipeline = str(msg.get("pipeline", ""))
    error = graph.validate_repo(graph.repo_settings({"repo": raw}))
    try:
        if error:
            out = {"ok": False, "message": error[0].upper() + error[1:] + "."}
        else:
            target = config.resolve_repo(cfg, raw)
            out = await asyncio.to_thread(workspace.check_repo, cfg, target)
            if target.is_default:
                out["message"] = "The factory's own test project (from factory.toml). " + out["message"]
    except Exception as exc:
        out = {"ok": False, "message": str(exc)}
    with contextlib.suppress(Exception):
        await ws.send_json({"type": "repo_check", "pipeline": pipeline, **out})


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "seq": store.last_seq()}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    hub.add(ws)
    background: set[asyncio.Task] = set()
    try:
        await ws.send_json(factory.snapshot())
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type") if isinstance(msg, dict) else None
            error = None
            if kind == "submit_work":
                text = str(msg.get("text", "")).strip()[:MAX_SPEC_CHARS]
                if text:
                    pipeline = str(msg.get("pipeline", ""))
                    if not await factory.submit(text, pipeline=pipeline if pipeline in factory.lines else ""):
                        error = "no pipeline takes typed specs"
                else:
                    error = "submit_work needs some text"
            elif kind in ("intake_list", "intake_pull", "intake_check"):
                task = asyncio.create_task(intake_command(ws, msg))
                background.add(task)
                task.add_done_callback(background.discard)
            elif kind == "check_tools":
                task = asyncio.create_task(factory.check_tools())
                background.add(task)
                task.add_done_callback(background.discard)
            elif kind == "check_agent":
                factory.health.poke()
            elif kind == "save_pipelines":
                error = await factory.set_pipelines(msg.get("pipelines"))
                if not error:
                    sync_feeds()
            elif kind == "check_repo":
                task = asyncio.create_task(repo_check(ws, msg))
                background.add(task)
                task.add_done_callback(background.discard)
            elif kind == "save_line":
                error = await factory.set_line(msg.get("machines"))
            elif kind in ("approve", "reject", "scrap", "retry"):
                try:
                    item_id = int(msg.get("item_id"))
                except (TypeError, ValueError):
                    item_id = None
                note = str(msg.get("note", "")).strip()[:MAX_SPEC_CHARS]
                if item_id is None:
                    error = f"{kind} needs an item_id"
                elif kind == "retry":
                    error = await factory.retry(item_id, note)
                else:
                    error = await factory.decide(item_id, kind, note)
            else:
                error = f"unknown command: {kind}"
            if error:
                await ws.send_json({"type": "error", "message": error})
    except WebSocketDisconnect:
        pass
    finally:
        hub.remove(ws)
