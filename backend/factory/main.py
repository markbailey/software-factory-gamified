"""FastAPI app: one WebSocket that the Godot game connects to."""
from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

import asyncio
import dataclasses

from . import config, graph, health, intake, janitor, runner, workspace
from .alerts import Alerts
from .engine import Factory
from .hub import Hub
from .store import Store

cfg = config.load()
cfg.workspace.mkdir(parents=True, exist_ok=True)
store = Store(cfg.db_path)
hub = Hub(store)
factory = Factory(cfg, store, hub)
alerts = Alerts(cfg.alerts, store)
hub.listeners.append(alerts.listen)
factory.health = health.AgentHealth(health.HealthConfig.from_toml(), cfg.allowed_commands, cfg.workspace,
                                    hub.broadcast)

MAX_SPEC_CHARS = 20000  # a pasted PRD can be long
# Intake pollers, for the game's intake window: "jira" and "pr_review" by source; GitHub issues
# and PRDs per pipeline, from that pipeline's repo ("github:<pipeline id>", "prd:<pipeline id>").
feeds: dict[str, intake.Feed] = {}
feed_tasks: dict[str, asyncio.Task] = {}
REPO_SOURCES = ("github", "prd", "github_pr")


def feed_for(source: str, pipeline: str, storage: str = "") -> intake.Feed | None:
    """The poller a pipeline's intake window shows for this source (of this storage)."""
    line = factory.lines.get(pipeline)
    if line is not None and "storages" in line.graph:
        if not storage:  # the line's first storage of it
            storage = next((str(st.get("id")) for st in graph.fed_storages(line.graph) if st.get("source") == source), "")
        return feeds.get(f"{source}:{pipeline}:{storage}") or (feeds.get(source) if source in ("jira", "pr_review") else None)
    if source in REPO_SOURCES:
        line = factory.lines.get(pipeline)
        if line is not None and source in line.sources:
            return feeds.get(f"{source}:{pipeline}")
        owner = factory.line_for_source(source)
        return feeds.get(f"{source}:{owner.id}") if owner is line and owner is not None else None
    return feeds.get(f"{source}:{pipeline}") or feeds.get(source)  # a line's own JQL, or the factory's


def _want_storages(line, wanted: dict) -> None:
    """The feeds a line's storages need: one per storage ("<source>:<line>:<storage>"), reading
    its own repo (or the line's) with its own filter."""
    only = graph.check_only(line.graph)
    for st in graph.fed_storages(line.graph):
        source, sid = str(st.get("source", "")), str(st.get("id", ""))
        query = " ".join(str(st.get("query") or "").split())[:1000]
        gh_repo = graph.github_name(str(st.get("repo") or "")) or line.repo.github
        key = f"{source}:{line.id}:{sid}"
        if source in ("github", "prd") and gh_repo:
            base = cfg.github if source == "github" else cfg.prd
            if base is not None:
                wanted[key] = (source, line, dataclasses.replace(base, repo=gh_repo), query if source == "github" else "",
                               only and source == "github")
        elif source == "github_pr" and gh_repo:
            wanted[key] = (source, line, dataclasses.replace(cfg.github, repo=gh_repo),
                           query or intake.DEFAULT_PR_SEARCH, True)
        elif source == "jira" and query and cfg.jira.base_url:
            wanted[key] = (source, line, dataclasses.replace(cfg.jira, jql=query), query, only)


def _tag_storage(key: str, adapter) -> None:
    """Tell a storage's feed which storage it is, and (Jira) the repo its crates are worked on."""
    if key.count(":") < 2:
        return
    adapter.storage = key.split(":")[2]  # crates remember the storage they came out of
    line = factory.lines.get(key.split(":")[1])
    st = next((s for s in graph.fed_storages(line.graph) if s.get("id") == adapter.storage), {}) if line else {}
    if adapter.source == "jira":
        adapter.work_repo = graph.github_name(str(st.get("repo") or ""))


def _start_feed(key: str, adapter, seconds: float) -> None:
    _tag_storage(key, adapter)
    factory.notifiers.append(adapter.notify)
    feed = feeds[key] = intake.Feed(adapter, seconds)
    feed.paused = lambda: factory.intake_paused(str(getattr(adapter, "pipeline", "") or ""))
    feed_tasks[key] = asyncio.create_task(feed.run())


def _stop_feed(key: str) -> None:
    feed = feeds.pop(key, None)
    task = feed_tasks.pop(key, None)
    if task:
        task.cancel()
    if feed and feed.adapter.notify in factory.notifiers:
        factory.notifiers.remove(feed.adapter.notify)


def _signature(adapter) -> tuple:
    """What a per-line feed watches: (repo, its own search, check only)."""
    query = getattr(adapter, "query", None)
    if query is None:  # Jira: its JQL is its search
        query = adapter.cfg.jql if getattr(adapter, "pipeline", "") else ""
    return (getattr(adapter.cfg, "repo", ""), query, bool(getattr(adapter, "check_only", False)))


def sync_feeds() -> None:
    """Watch the issues (and PRDs) of each pipeline's repo, and the Jira issues of each pipeline
    with its own JQL: start pollers for pipelines that take them and stop ones whose pipeline,
    repo or search changed. Run at startup and after a save."""
    wanted: dict[str, tuple] = {}
    for line in factory.lines.values():
        if "storages" in line.graph:
            _want_storages(line, wanted)
            continue
        repo = line.repo
        queries = graph.queries_of(line.graph)
        only = graph.check_only(line.graph)
        # A storage of GitHub issues belted into the intake is as good as enabled = true.
        stored = "storages" in line.graph and "github" in line.sources
        for source, base in (("github", cfg.github if cfg.github.enabled or queries.get("github") or stored else None),
                             ("prd", cfg.prd)):
            # A pipeline takes GitHub issues if its intake says so, or (for "github") if it's the one
            # they fall back to.
            gh_repo = graph.storage_repo(line.graph, source) or repo.github  # the storage's own, or the line's
            if base is None or not gh_repo:
                continue
            if source not in line.sources and factory.line_for_source(source) is not line:
                continue
            query = queries.get("github", "") if source == "github" else ""
            wanted[f"{source}:{line.id}"] = (source, line, dataclasses.replace(base, repo=gh_repo), query,
                                             only and source == "github")
        pr_repo = graph.storage_repo(line.graph, "github_pr") or repo.github
        if "github_pr" in line.sources and pr_repo:
            wanted[f"github_pr:{line.id}"] = ("github_pr", line, dataclasses.replace(cfg.github, repo=pr_repo),
                                              queries.get("github_pr", "") or intake.DEFAULT_PR_SEARCH, True)
        if "jira" in line.sources and queries.get("jira") and cfg.jira.base_url:
            wanted[f"jira:{line.id}"] = ("jira", line, dataclasses.replace(cfg.jira, jql=queries["jira"]),
                                         queries["jira"], only)
    factory.jira_lines = {key.split(":")[1] for key in wanted if key.startswith("jira:")}
    for key, feed in feeds.items():  # a storage's repo may have changed without its search
        _tag_storage(key, feed.adapter)
    if "pr_review" not in feeds and any("pr_review" in line.sources for line in factory.lines.values()):
        # A storage of PRs to review: watch them even if factory.toml has them switched off.
        reviews = intake.ReviewIntakeConfig.from_toml()
        _start_feed("pr_review", intake.ReviewRequestIntake(reviews, factory), reviews.poll_seconds)
        print(f"[intake] watching pull requests waiting for review by {reviews.reviewer}")
    for key in list(feeds):
        if ":" in key:  # per-line feeds (the factory-wide Jira and review feeds have no line)
            have = feeds[key].adapter
            want = wanted.get(key)
            same = want is not None and _signature(have) == (getattr(want[2], "repo", ""), want[3], want[4])
            if not same:
                _stop_feed(key)
    for key, (source, line, settings, query, only) in wanted.items():
        if key in feeds:
            continue
        if source == "jira":
            _start_feed(key, intake.JiraIntake(settings, factory, pipeline=line.id, check_only=only),
                        settings.poll_seconds)
            print(f"[intake] {line.name}: watching Jira issues matching {query}" + (" (checking only)" if only else ""))
            continue
        if source == "github_pr":
            _start_feed(key, intake.GitHubPrIntake(settings, factory, pipeline=line.id, query=query),
                        settings.poll_seconds)
            print(f"[intake] {line.name}: checking open PRs in {settings.repo} matching {query}")
            continue
        default = settings.repo.lower() == cfg.default_repo.github.lower()
        if source == "github":
            adapter = intake.GitHubIntake(settings, factory, pipeline=line.id, default=default, query=query,
                                          check_only=only)
        else:
            adapter = intake.PrdIntake(settings, factory, pipeline=line.id, default=default)
        _start_feed(key, adapter, settings.poll_seconds)
        what = (f"matching {query}" if query else f"labelled '{settings.label}'") + \
            (" (PRDs to plan)" if source == "prd" else "") + (" (checking only)" if only and source == "github" else "")
        print(f"[intake] {line.name}: watching GitHub issues in {settings.repo} {what}")


SOURCE_SECTIONS = {"github": "intake.github", "jira": "intake.jira", "pr_review": "intake.review_requests",
                   "prd": "intake.prd", "github_pr": "intake.github"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        for note in workspace.ensure_repo(cfg):
            print(f"[factory] {note}")
        factory.mark_prepared(cfg.default_repo)
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
    sync_feeds()
    waiting = factory.resume_waiting()
    resumed, scrapped = factory.recover_interrupted()
    if resumed:
        print(f"[factory] {len(resumed)} crate(s) the last run left on the line carry on where they were")
    if scrapped:
        print(f"[factory] {len(scrapped)} crate(s) left on a line that can't run now went to its scrap bin")
    factory.backfill_pr_states()
    if waiting:
        print(f"[factory] {waiting} crate(s) still waiting for your approval")
    print(f"[factory] game should connect to ws://{cfg.host}:{cfg.port}/ws")
    yield
    # Stop running commands first, so none of them is recorded as a failure: the crates stay
    # where they are and carry on when the backend starts again.
    runner.stop_all()
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
    if "storages" in line.graph:  # one listing per storage, tagged with its id
        wanted = ([("game", "")] if "game" in line.sources else []) + [
            (str(st.get("source", "")), str(st.get("id", ""))) for st in graph.fed_storages(line.graph)]
    else:
        wanted = [(s, "") for s in ("game", "github", "jira", "pr_review", "prd", "github_pr")
                  if s in line.sources or feed_for(s, pipeline) is not None]
    jobs = []
    for source, storage in wanted:
        feed = feed_for(source, pipeline, storage)
        if source == "game":
            jobs.append(asyncio.sleep(0, {"source": "game", "enabled": True, "items": [],
                                          "about": "Specs you type in the box at the top of the game."}))
        elif feed is not None:
            jobs.append(feed.listing())
        elif source in REPO_SOURCES and not (graph.storage_repo(line.graph, source) or line.repo.github) and (
                source == "github_pr" or (cfg.github.enabled if source == "github" else cfg.prd is not None)):
            jobs.append(asyncio.sleep(0, {"source": source, "enabled": False, "items": [], "about":
                "This pipeline's repo isn't on GitHub, so there are no issues to watch. Give its storage a "
                "GitHub repo, or set one in the intake's settings."}))
        elif source == "jira" and "storages" in line.graph and cfg.jira.base_url:
            jobs.append(asyncio.sleep(0, {"source": source, "enabled": False, "items": [], "about":
                "Give this storage a JQL filter in build mode (or set enabled = true under [intake.jira] "
                "in backend/factory.toml to use the JQL there)."}))
        else:
            jobs.append(asyncio.sleep(0, {"source": source, "enabled": False, "items": [], "about":
                f"Turned off: set enabled = true under [{SOURCE_SECTIONS.get(source, 'intake')}] "
                f"in backend/factory.toml and restart the backend."}))
    out["sources"] = list(await asyncio.gather(*jobs))
    for entry, (_, storage) in zip(out["sources"], wanted):
        entry["storage"] = storage
    return out


async def intake_command(ws: WebSocket, msg: dict) -> None:
    """intake_list / intake_pull / intake_check, run beside the socket loop (they call gh)."""
    kind = msg.get("type")
    pipeline = str(msg.get("pipeline", ""))
    source = str(msg.get("source", ""))
    storage = str(msg.get("storage", ""))
    try:
        if kind == "intake_pull":
            feed = feed_for(source, pipeline, storage)
            if feed is None:
                raise RuntimeError(f"{source or 'that source'} isn't switched on")
            item = await feed.pull(str(msg.get("ref", "")))
            if item is None:
                raise RuntimeError("no pipeline takes crates from there")
        elif kind == "intake_check":
            line = factory.lines.get(pipeline)
            if line is not None and "storages" in line.graph:
                pairs = [(str(st.get("source")), str(st.get("id"))) for st in graph.fed_storages(line.graph)
                         if (not source or st.get("source") == source) and (not storage or st.get("id") == storage)]
                targets = [f for f in (feed_for(s, pipeline, sid) for s, sid in pairs) if f is not None]
            else:
                sources = [source] if source else ["github", "jira", "pr_review", "prd", "github_pr"]
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
            elif kind == "belt_speed":
                error = await factory.set_belt_speed(msg.get("speed"))
            elif kind == "pause":
                error = await factory.set_paused(str(msg.get("target", "")), str(msg.get("id", "")),
                                                 bool(msg.get("paused", True)))
            elif kind == "stats":
                try:
                    days = int(msg.get("days", 7))
                except (TypeError, ValueError):
                    days = 7
                await ws.send_json(factory.stats(days))
            elif kind == "get_log":
                try:
                    item = factory.store.get_item(int(msg.get("item_id")))
                except (TypeError, ValueError):
                    item = None
                if item:
                    await ws.send_json({"type": "item_log", "item_id": item["id"], "log": item["log"]})
                else:
                    error = "get_log needs a crate's item_id"
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
