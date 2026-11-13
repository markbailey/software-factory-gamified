"""FastAPI app: one WebSocket that the Godot game connects to."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

import asyncio

from . import config, intake, workspace
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
    for adapter, seconds in adapters:
        factory.notifiers.append(adapter.notify)
        pollers.append(asyncio.create_task(intake.run_poller(adapter, seconds)))
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
            if kind == "submit_work":
                text = str(msg.get("text", "")).strip()[:MAX_SPEC_CHARS]
                if text:
                    await factory.submit(text)
                else:
                    await ws.send_json({"type": "error", "message": "submit_work needs some text"})
            else:
                await ws.send_json({"type": "error", "message": f"unknown command: {kind}"})
    except WebSocketDisconnect:
        pass
    finally:
        hub.remove(ws)
