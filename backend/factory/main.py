"""FastAPI app: one WebSocket that the Godot game connects to."""
from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

import asyncio
import copy
import dataclasses

from . import accounts, config, graph, health, intake, janitor, runner, workspace
from .alerts import Alerts
from .engine import Factory
from .hub import Hub
from .store import Store

cfg = config.load()
# The factory's own GitHub / Jira accounts (backend/accounts.toml), before anything runs gh or git.
factory_accounts = accounts.load()
accounts.use(factory_accounts, cfg.jira)
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
        elif source == "github_pr" and graph.lists_repos(st):  # every repo in the gear's list
            wanted[key] = (source, line, dataclasses.replace(cfg.github, repo=intake.LISTED_REPOS),
                           query or intake.DEFAULT_PR_SEARCH, True)
        elif source == "github_pr" and gh_repo:
            wanted[key] = (source, line, dataclasses.replace(cfg.github, repo=gh_repo),
                           query or intake.DEFAULT_PR_SEARCH, True)
        elif source == "jira" and (query or not cfg.jira.enabled):
            # A storage with no JQL of its own reads factory.toml's: through the factory-wide feed
            # when that's on, or its own feed with that JQL when it isn't.
            jql = query or cfg.jira.jql
            wanted[key] = (source, line, dataclasses.replace(cfg.jira, jql=jql), jql, only)


def _tag_storage(key: str, adapter) -> None:
    """Tell a storage's feed which storage it is, and (Jira) the repo its crates are worked on."""
    if key.count(":") < 2:
        return
    adapter.storage = key.split(":")[2]  # crates remember the storage they came out of
    line = factory.lines.get(key.split(":")[1])
    st = next((s for s in graph.fed_storages(line.graph) if s.get("id") == adapter.storage), {}) if line else {}
    if adapter.source == "jira":
        adapter.work_repo = graph.github_name(str(st.get("repo") or ""))
    if adapter.source == "github_pr":  # read on every save, so the tick needs no restart of the feed
        adapter.include_factory_prs = graph.includes_factory_prs(st)


def want_account(key: str) -> str:
    """The account a feed works as: its storage's pick, else its line's, else "" (the default)."""
    source, _, rest = key.partition(":")
    service = "jira" if source == "jira" else "github"
    pid, _, sid = rest.partition(":")
    line = factory.lines.get(pid)
    if line is None:
        return ""
    st = next((s for s in graph.storages_of(line.graph) if s.get("id") == sid), {}) if sid else {}
    return str(st.get("account") or "") or graph.account_of(line.graph, service)


def _start_feed(key: str, adapter, seconds: float) -> None:
    adapter.account = want_account(key)
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
        if "jira" in line.sources and queries.get("jira"):
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
            same = (want is not None and _signature(have) == (getattr(want[2], "repo", ""), want[3], want[4])
                    and getattr(have, "account", "") == want_account(key))
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
        if source == "github_pr" and settings.repo == intake.LISTED_REPOS:
            _start_feed(key, intake.ListedReposPrIntake(settings, factory, pipeline=line.id, query=query),
                        settings.poll_seconds)
            print(f"[intake] {line.name}: checking open PRs in the factory's repo list matching {query}")
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
    for line in factory.lines.values():
        if line.repo.unset:
            continue
        error = await factory.prepare_repos([line])
        print(f"[factory] {error}" if error else f"[factory] {line.name} works on {line.repo.github or line.repo.path}"
              f" ({line.repo.base_branch})")
    print(f"[accounts] {len(factory_accounts['github'])} GitHub and {len(factory_accounts['jira'])} Jira account(s); "
          f"by default GitHub uses {accounts.label('github')} and Jira {accounts.label('jira')}")
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
    pollers.append(asyncio.create_task(custom_queue_loop()))
    if store.queue_keys():
        print(f"[intake] {sum(len(store.queue_list(p, s)) for p, s in store.queue_keys())} change(s) "
              "queued in Custom storages go onto their lines as they take them")
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
            (str(st.get("source", "")), str(st.get("id", ""))) for st in graph.fed_storages(line.graph, with_off=True)]
        off = {str(st.get("id", "")) for st in graph.fed_storages(line.graph, with_off=True) if not graph.storage_on(st)}
    else:
        wanted = [(s, "") for s in ("game", "github", "jira", "pr_review", "prd", "github_pr")
                  if s in line.sources or feed_for(s, pipeline) is not None]
        off = set()
    jobs = []
    for source, storage in wanted:
        feed = feed_for(source, pipeline, storage)
        if storage in off and source != "custom":  # switched off: not checked at all
            jobs.append(asyncio.sleep(0, {"source": source, "enabled": False, "disabled": True, "items": [], "about":
                "Switched off: it isn't checked and brings nothing in. Turn it on to start it again."}))
        elif source == "custom":
            jobs.append(asyncio.sleep(0, custom_listing(line, storage)))
        elif source == "game":
            jobs.append(asyncio.sleep(0, {"source": "game", "enabled": True, "items": [],
                                          "about": "Specs typed into this intake. Describe one below and press Add."}))
        elif feed is not None:
            jobs.append(feed.listing())
        elif source in REPO_SOURCES and not (graph.storage_repo(line.graph, source) or line.repo.github) and (
                source == "github_pr" or (cfg.github.enabled if source == "github" else cfg.prd is not None)):
            jobs.append(asyncio.sleep(0, {"source": source, "enabled": False, "items": [], "about":
                "This pipeline's repo isn't on GitHub, so there are no issues to watch. Give its storage a "
                "GitHub repo, or set one in the intake's settings."}))
        elif source == "jira" and accounts.jira_ready(accounts.jira_creds(graph.account_of(line.graph, "jira"))):
            jobs.append(asyncio.sleep(0, {"source": source, "enabled": False, "items": [], "about":
                accounts.jira_ready(accounts.jira_creds(graph.account_of(line.graph, "jira")))}))
        else:
            jobs.append(asyncio.sleep(0, {"source": source, "enabled": False, "items": [], "about":
                f"Turned off: set enabled = true under [{SOURCE_SECTIONS.get(source, 'intake')}] "
                f"in backend/factory.toml and restart the backend."}))
    out["sources"] = list(await asyncio.gather(*jobs))
    for entry, (_, storage) in zip(out["sources"], wanted):
        entry["storage"] = storage
        if storage in off and entry.get("source") == "custom":  # its queue waits while it's off
            entry.update(disabled=True, about="Switched off: its queue waits and nothing goes onto the line. "
                                              "Turn it on to start it again.")
    return out


def _refs(msg: dict) -> list[str]:
    raw = msg.get("refs") if isinstance(msg.get("refs"), list) else [msg.get("ref")] if msg.get("ref") else []
    return [str(r).strip() for r in raw if str(r or "").strip()][:100]


async def close_storage_items(pipeline: str, source: str, storage: str, refs: list[str], comment: str = "",
                              reason: str = "", transition: str = "") -> dict:
    """Close issues or PRs sitting in a storage (Jira: move them to a done status), as the account
    that storage works as. For the game's storage window and anything else (the dashboard).
    Returns an intake_closed message."""
    feed = feed_for(source, pipeline, storage)
    if feed is None:
        raise RuntimeError(f"{source or 'that storage'} isn't switched on")
    if not refs:
        raise RuntimeError("pick something to close")
    done = await feed.close(refs, comment[:MAX_SPEC_CHARS], reason, transition)
    return {"type": "intake_closed", "pipeline": pipeline, "source": source, "storage": storage,
            "account": feed.account_label(), **done}


async def intake_command(ws: WebSocket, msg: dict) -> None:
    """intake_list / intake_pull / intake_check / intake_close / intake_close_options, run beside
    the socket loop (they call gh or Jira)."""
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
        elif kind == "intake_close":
            await ws.send_json(await close_storage_items(
                pipeline, source, storage, _refs(msg), str(msg.get("comment", "") or ""),
                str(msg.get("reason", "") or ""), str(msg.get("transition", "") or "")))
        elif kind == "intake_close_options":
            feed = feed_for(source, pipeline, storage)
            if feed is None:
                raise RuntimeError(f"{source or 'that storage'} isn't switched on")
            await ws.send_json({"type": "intake_close_options", "pipeline": pipeline, "source": source,
                                "storage": storage, "refs": _refs(msg), "account": feed.account_label(),
                                **await feed.close_options(_refs(msg))})
            return  # nothing changed, so no fresh listing
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
        target = None if error else config.resolve_repo(cfg, raw)
        if error:
            out = {"ok": False, "message": error[0].upper() + error[1:] + "."}
        elif target.unset:
            out = {"ok": False, "message": "Pick a repo for this line: there's no default repo."}
        else:
            out = await asyncio.to_thread(workspace.check_repo, cfg, target)
    except Exception as exc:
        out = {"ok": False, "message": str(exc)}
    with contextlib.suppress(Exception):
        await ws.send_json({"type": "repo_check", "pipeline": pipeline, **out})


async def submit_custom(text: str, pipeline: str, storage: str) -> str | None:
    """A change typed into a Custom storage's window: a crate on the line it's belted into,
    worked on in the storage's repo if it has one."""
    line = factory.lines.get(pipeline)
    st = next((s for s in graph.fed_storages(line.graph) if s.get("id") == storage), None) if line else None
    if st is None or st.get("source") != "custom":
        return "that storage isn't a Custom storage belted into an intake (save the layout in build mode first)"
    if await factory.submit(text, pipeline=line.id, storage=storage,
                            repo=graph.github_name(str(st.get("repo") or ""))) is None:
        return "that line isn't taking crates right now"
    return None


CUSTOM_RELEASE_SECONDS = 2.0  # how often a Custom storage's queue looks for room on its line


def _custom_storage(pipeline: str, storage: str):
    """(line, storage settings) for a Custom storage belted into a line's intake, else (line, None)."""
    line = factory.lines.get(pipeline)
    st = next((s for s in graph.fed_storages(line.graph, with_off=True) if s.get("id") == storage), None) if line else None
    return line, (st if st is not None and st.get("source") == "custom" else None)


async def queue_custom(text: str, pipeline: str, storage: str) -> str | None:
    """A change typed into a Custom storage's window joins the end of its queue; the queue goes
    onto the line one crate at a time (straight away if the line takes it now)."""
    _, st = _custom_storage(pipeline, storage)
    if st is None:
        return "that storage isn't a Custom storage belted into an intake (save the layout in build mode first)"
    store.queue_add(pipeline, storage, text)
    await release_custom(pipeline, storage)
    return None


async def release_custom(pipeline: str, storage: str) -> bool:
    """Send the next change in a Custom storage's queue onto its line, if the line takes one now:
    it isn't paused or full, and the last crate out of this storage has left the intake. The
    crate is worked on in the storage's repo, as the storage's account. True if one went."""
    line, st = _custom_storage(pipeline, storage)
    if st is None or not graph.storage_on(st) or factory.intake_paused(line.id) or store.at_intake(line.id, storage):
        return False  # (a storage taken off the line keeps its queue, for when it's belted in again)
    queue = store.queue_list(pipeline, storage)
    if not queue:
        return False
    if await submit_custom(queue[0]["text"], pipeline, storage) is not None:
        return False
    store.queue_remove(queue[0]["id"])
    return True


async def custom_queue_loop() -> None:
    while True:
        await asyncio.sleep(CUSTOM_RELEASE_SECONDS)
        for pipeline, storage in store.queue_keys():
            try:
                await release_custom(pipeline, storage)
            except Exception as exc:
                print(f"[intake] couldn't send a queued change from storage {storage}: {exc}")


def custom_queue_command(msg: dict) -> str | None:
    """Change a Custom storage's queue: edit an entry's text, remove it, or move it (delta places)."""
    kind = str(msg.get("type"))
    try:
        entry_id = int(msg.get("id"))
    except (TypeError, ValueError):
        return f"{kind} needs the queued change's id"
    if kind == "custom_queue_edit":
        text = str(msg.get("text", "")).strip()[:MAX_SPEC_CHARS]
        if not text:
            return "a queued change needs some text (remove it instead)"
        done = store.queue_edit(entry_id, text)
    elif kind == "custom_queue_remove":
        done = store.queue_remove(entry_id)
    else:
        try:
            done = store.queue_move(entry_id, int(msg.get("delta", 0)))
        except (TypeError, ValueError):
            return "custom_queue_move needs a delta"
    return None if done else "that change has already gone onto the line"


def custom_listing(line, storage: str) -> dict:
    """A Custom storage's window: its queue (next first), then the crates typed into it lately."""
    rows = []
    queue = store.queue_list(line.id, storage)
    for n, entry in enumerate(queue, 1):
        text = str(entry["text"]).strip()
        rows.append({"ref": f"q{entry['id']}", "label": "Next" if n == 1 else f"{n}.",
                     "title": text.splitlines()[0][:200] if text else "", "text": text, "url": "", "by": "",
                     "tags": [], "auto": False, "can_pull": False, "item_id": None, "queue_id": entry["id"],
                     "queue_pos": n, "queue_len": len(queue)})
    for item in reversed(store.list_items(log_tail=0)):
        if item.get("pipeline") != line.id or item.get("storage") != storage:
            continue
        title = str(item.get("text", "")).strip().splitlines()[0][:200] if str(item.get("text", "")).strip() else ""
        rows.append({"ref": str(item["id"]), "label": f"#{item['id']}", "title": title, "url": "", "by": "",
                     "tags": [], "auto": False, "can_pull": False, "item_id": item["id"],
                     "item_state": item.get("state", "")})
        if len(rows) >= len(queue) + 20:
            break
    return {"source": "custom", "enabled": True, "can_type": True, "items": rows,
            "about": "Changes you describe. Type one below and press Add: it joins the queue, and the queue goes "
                     "onto this line one crate at a time as the line takes them."}


async def set_storage_enabled(pipeline: str, storage: str, on: bool) -> str | None:
    """Switch a storage on or off from its window: saved with the layout, like a change in build
    mode. One switched off isn't checked and brings nothing in. Returns an error, or None."""
    layout = copy.deepcopy(factory.layout)
    p = next((q for q in layout if str(q.get("id", "")) == pipeline), None)
    st = next((s for s in graph.storages_of(p) if str(s.get("id", "")) == storage), None) if p else None
    if st is None:
        return f"there's no storage {storage!r} on that line (save the layout in build mode first)"
    if graph.storage_on(st) == on:
        return None
    if on:
        st.pop("enabled", None)
    else:
        st["enabled"] = False
    error = await factory.set_pipelines(layout)
    if error:
        return error
    sync_feeds()
    print(f"[intake] {p.get('name', pipeline)}: storage {storage} switched {'on' if on else 'off'}")
    return None


def restart_feeds() -> None:
    """After the accounts change: start every feed again, so each works as its (new) account."""
    for key in list(feeds):
        _stop_feed(key)
    if cfg.jira.enabled:
        _start_feed("jira", intake.JiraIntake(cfg.jira, factory), cfg.jira.poll_seconds)
    reviews = intake.ReviewIntakeConfig.from_toml()
    if reviews.enabled:
        _start_feed("pr_review", intake.ReviewRequestIntake(reviews, factory), reviews.poll_seconds)
    sync_feeds()


ACCOUNT_COMMANDS = ("get_accounts", "save_account", "remove_account", "set_default_account", "check_accounts")


async def accounts_command(ws: WebSocket, msg: dict) -> None:
    """The Accounts window: add, change and remove the factory's GitHub / Jira accounts, pick each
    service's default, or check who they are. Tokens only ever come in from the game."""
    global factory_accounts
    kind = msg.get("type")
    service = str(msg.get("service", ""))
    out: dict = {}
    try:
        new = None
        if kind == "save_account":
            new, out["saved"] = accounts.upsert(factory_accounts, service, msg.get("account") or {})
        elif kind == "remove_account":
            new = accounts.remove(factory_accounts, service, str(msg.get("id", "")))
        elif kind == "set_default_account":
            new = accounts.set_default(factory_accounts, service, str(msg.get("id", "")))
        if new is not None:
            accounts.save(new)
            factory_accounts = new
            accounts.use(factory_accounts, cfg.jira)
            restart_feeds()
            print(f"[accounts] saved: by default GitHub uses {accounts.label('github')}, "
                  f"Jira {accounts.label('jira')}")
        if kind in ("check_accounts", "save_account"):
            out["check"] = await asyncio.to_thread(accounts.check, factory_accounts)
    except Exception as exc:
        out["error"] = str(exc)
    with contextlib.suppress(Exception):
        await ws.send_json({"type": "accounts", **accounts.summary(factory_accounts), **out})


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
                    storage = str(msg.get("storage", ""))
                    if storage:  # a change typed into a Custom storage: into its queue for the line
                        error = await queue_custom(text, pipeline, storage)
                    elif not await factory.submit(text, pipeline=pipeline if pipeline in factory.lines else ""):
                        error = "no pipeline takes typed specs"
                else:
                    error = "submit_work needs some text"
            elif kind in ("custom_queue_edit", "custom_queue_remove", "custom_queue_move"):
                error = custom_queue_command(msg)
            elif kind in ("intake_list", "intake_pull", "intake_check", "intake_close", "intake_close_options"):
                task = asyncio.create_task(intake_command(ws, msg))
                background.add(task)
                task.add_done_callback(background.discard)
            elif kind == "check_tools":
                task = asyncio.create_task(factory.check_tools())
                background.add(task)
                task.add_done_callback(background.discard)
            elif kind == "belt_speed":
                error = await factory.set_belt_speed(msg.get("speed"))
            elif kind == "agent_defaults":
                error = await factory.set_agent_defaults(msg.get("model"), msg.get("effort"))
            elif kind == "force_delete":
                error = await factory.set_force_delete(msg.get("on"))
            elif kind == "branch_pattern":
                error = await factory.set_branch_pattern(msg.get("pattern"))
            elif kind in ("worktrees", "worktrees_clean"):  # the dashboard's Worktrees & branches tab
                task = asyncio.create_task(factory.worktrees_command(ws, msg))
                background.add(task)
                task.add_done_callback(background.discard)
            elif kind == "set_repos":
                error = await factory.set_repos(msg.get("repos"))
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
            elif kind == "storage_enabled":
                error = await set_storage_enabled(str(msg.get("pipeline", "")), str(msg.get("storage", "")),
                                                  bool(msg.get("enabled", True)))
            elif kind == "save_pipelines":
                error = await factory.set_pipelines(msg.get("pipelines"))
                if not error:
                    sync_feeds()
            elif kind in ACCOUNT_COMMANDS:
                task = asyncio.create_task(accounts_command(ws, msg))
                background.add(task)
                task.add_done_callback(background.discard)
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
