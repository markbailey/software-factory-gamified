"""FastAPI app: one WebSocket that the Godot game connects to."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

import asyncio

from . import config, intake, janitor, workspace
from .engine import Factory
from .hub import Hub
from .store import Store

cfg = config.load()
cfg.workspace.mkdir(parents=True, exist_ok=True)
store = Store(cfg.db_path)
hub = Hub(store)
factory = Factory(cfg, store, hub)

MAX_SPEC_CHARS = 4000


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
    pollers = []
    adapters = []
    if cfg.github.enabled:
        adapters.append((intake.GitHubIntake(cfg.github, factory), cfg.github.poll_seconds))
        print(f"[intake] watching GitHub issues in {cfg.github.repo} labelled '{cfg.github.label}'")
    if cfg.jira.enabled:
        adapters.append((intake.JiraIntake(cfg.jira, factory), cfg.jira.poll_seconds))
        print(f"[intake] watching Jira: {cfg.jira.jql}")
    pollers.append(asyncio.create_task(
        janitor.run(cfg, store, cfg.cleanup_interval_minutes, cfg.keep_scrapped_hours)))
    for adapter, seconds in adapters:
        factory.notifiers.append(adapter.notify)
        pollers.append(asyncio.create_task(intake.run_poller(adapter, seconds)))
    waiting = factory.resume_waiting()
    if waiting:
        print(f"[factory] {waiting} crate(s) still waiting for your approval")
    print(f"[factory] game should connect to ws://{cfg.host}:{cfg.port}/ws")
    yield
    for task in pollers:
        task.cancel()


app = FastAPI(title="Software Factory", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "seq": store.last_seq()}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    hub.add(ws)
    try:
        await ws.send_json(factory.snapshot())
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type") if isinstance(msg, dict) else None
            error = None
            if kind == "submit_work":
                text = str(msg.get("text", "")).strip()[:MAX_SPEC_CHARS]
                if text:
                    await factory.submit(text)
                else:
                    error = "submit_work needs some text"
            elif kind == "save_line":
                error = await factory.set_line(msg.get("machines"))
            elif kind in ("approve", "reject", "retry"):
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
