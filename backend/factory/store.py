"""SQLite work store: items and the event log. The backend owns the truth here."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

MAX_LOG_LINES = 500
TERMINAL_STATES = ("shipped", "scrapped")
# States that survive a backend restart: finished crates, and crates waiting for you at a gate.
RESUMABLE_STATES = TERMINAL_STATES + ("awaiting_approval",)


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                state TEXT NOT NULL,
                location TEXT NOT NULL,
                created_at REAL NOT NULL,
                log TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS intake_seen (
                source TEXT NOT NULL,
                ref TEXT NOT NULL,
                item_id INTEGER,
                PRIMARY KEY (source, ref)
            );
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                at REAL NOT NULL,
                body TEXT NOT NULL
            );
            """
        )
        # Columns added in later slices: add them to older databases.
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(items)")}
        for col in ("source", "source_ref", "source_url", "pr_url", "notes", "review", "pipeline",
                    "head_sha", "risk", "risk_reason", "outcome", "scrap_kind", "waiting_for", "spec", "route"):
            if col not in have:
                self.db.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        if "loops" not in have:  # how many times the crate has been sent back for rework
            self.db.execute("ALTER TABLE items ADD COLUMN loops INTEGER NOT NULL DEFAULT 0")
        # Before pipelines, there was one line and places had no pipeline prefix.
        self.db.execute("UPDATE items SET pipeline = 'build' WHERE pipeline = ''")
        self.db.execute(
            "UPDATE items SET location = 'build:' || location WHERE location NOT LIKE '%:%' AND"
            " (location IN ('intake', 'dock', 'scrap') OR location LIKE 'belt\\_%' ESCAPE '\\'"
            " OR location LIKE 'return\\_%' ESCAPE '\\')")
        self.db.commit()

    # Items -----------------------------------------------------------------
    def create_item(self, text: str, source: str = "game", source_ref: str = "", source_url: str = "",
                    pipeline: str = "build", location: str = "", pr_url: str = "", head_sha: str = "") -> dict:
        cur = self.db.execute(
            "INSERT INTO items (text, state, location, created_at, source, source_ref, source_url,"
            " pipeline, pr_url, head_sha) VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?)",
            (text, location or f"{pipeline}:intake", time.time(), source, source_ref, source_url,
             pipeline, pr_url, head_sha),
        )
        self.db.commit()
        return self.get_item(cur.lastrowid)

    def get_item(self, item_id: int) -> dict:
        row = self.db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._row(row)

    def list_items(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM items ORDER BY id").fetchall()
        return [self._row(r) for r in rows]

    def update_item(self, item_id: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE items SET {cols} WHERE id = ?", (*fields.values(), item_id))
        self.db.commit()

    def append_log(self, item_id: int, line: str) -> None:
        row = self.db.execute("SELECT log FROM items WHERE id = ?", (item_id,)).fetchone()
        log = json.loads(row["log"])
        log.append(line)
        self.update_item(item_id, log=json.dumps(log[-MAX_LOG_LINES:]))

    def scrap_interrupted(self) -> list[int]:
        """Items left mid-pipeline by a previous run can't resume yet, so scrap them."""
        rows = self.db.execute(
            f"SELECT id, pipeline FROM items WHERE state NOT IN ({','.join('?' * len(RESUMABLE_STATES))})",
            RESUMABLE_STATES,
        ).fetchall()
        ids = [r["id"] for r in rows]
        for r in rows:
            self.append_log(r["id"], "[factory] backend restarted while this crate was in flight; scrapped")
            self.update_item(r["id"], state="scrapped", location=f"{r['pipeline'] or 'build'}:scrap")
        return ids

    # Intake ----------------------------------------------------------------
    def seen(self, source: str, ref: str) -> bool:
        row = self.db.execute("SELECT 1 FROM intake_seen WHERE source = ? AND ref = ?", (source, ref)).fetchone()
        return row is not None

    def seen_item(self, source: str, ref: str) -> int | None:
        """The crate an intake made for this issue or PR, if it made one."""
        row = self.db.execute("SELECT item_id FROM intake_seen WHERE source = ? AND ref = ?",
                              (source, ref)).fetchone()
        return int(row["item_id"]) if row is not None and row["item_id"] is not None else None

    def forget_seen(self, source: str, ref: str) -> None:
        """Let an intake take this issue again (after it was handed back to its author)."""
        self.db.execute("DELETE FROM intake_seen WHERE source = ? AND ref = ?", (source, ref))
        self.db.commit()

    def mark_seen(self, source: str, ref: str, item_id: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO intake_seen (source, ref, item_id) VALUES (?, ?, ?)",
                        (source, ref, item_id))
        self.db.commit()

    # Events ----------------------------------------------------------------
    def append_event(self, event: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO events (at, body) VALUES (?, ?)", (time.time(), json.dumps(event))
        )
        self.db.commit()
        return cur.lastrowid

    def last_seq(self) -> int:
        row = self.db.execute("SELECT MAX(seq) AS s FROM events").fetchone()
        return row["s"] or 0

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["log"] = json.loads(d["log"])
        return d
