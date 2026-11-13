"""Fans events out to every connected game client, stamping each with a sequence number."""
from __future__ import annotations

from fastapi import WebSocket

from .store import Store


class Hub:
    def __init__(self, store: Store):
        self.store = store
        self.clients: set[WebSocket] = set()

    def add(self, ws: WebSocket) -> None:
        self.clients.add(ws)

    def remove(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def emit(self, event: dict) -> None:
        await self.broadcast({**event, "seq": self.store.append_event(event)})

    async def broadcast(self, event: dict) -> None:
        """Send to every client without logging it (used for fresh snapshots)."""
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.remove(ws)
