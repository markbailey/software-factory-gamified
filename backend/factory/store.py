"""SQLite work store: items and the event log. The backend owns the truth here."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

MAX_LOG_LINES = 500
TERMINAL_STATES = ("shipped", "scrapped")
# States that survive a backend restart: finished crates, and crates waiting for you at a gate.
RESUMABLE_STATES = TERMINAL_STATES + ("awaiting_approval", "held")
# Crates on a belt or in a machine when the backend stopped: they carry on from there.
IN_FLIGHT_STATES = ("queued", "waiting", "running", "moving")


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
            CREATE TABLE IF NOT EXISTS intake_retries (
                source TEXT NOT NULL,
                ref TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (source, ref)
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                pipeline TEXT NOT NULL DEFAULT '',
                machine TEXT NOT NULL,
                machine_type TEXT NOT NULL DEFAULT '',
                started_at REAL NOT NULL,
                ended_at REAL NOT NULL,
                outcome TEXT NOT NULL,
                failed_step TEXT NOT NULL DEFAULT '',
                cost_usd REAL NOT NULL DEFAULT 0,
                turns INTEGER NOT NULL DEFAULT 0,
                model TEXT NOT NULL DEFAULT '',
                effort TEXT NOT NULL DEFAULT '',
                agent TEXT NOT NULL DEFAULT '',
                tokens INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS runs_by_item ON runs (item_id);
            CREATE INDEX IF NOT EXISTS runs_by_time ON runs (started_at);
            CREATE TABLE IF NOT EXISTS custom_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pipeline TEXT NOT NULL,
                storage TEXT NOT NULL,
                text TEXT NOT NULL,
                pos REAL NOT NULL,
                created_at REAL NOT NULL
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
                    "head_sha", "risk", "risk_reason", "outcome", "scrap_kind", "waiting_for", "spec", "route", "pr_state",
                    "pr_title", "pr_body", "repo", "storage", "branch",
                    "intake_sha"):  # intake_sha: an Open PRs crate's PR head when it came in
            if col not in have:
                self.db.execute(f"ALTER TABLE items ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        if "loops" not in have:  # how many times the crate has been sent back for rework
            self.db.execute("ALTER TABLE items ADD COLUMN loops INTEGER NOT NULL DEFAULT 0")
        if "finished_at" not in have:  # when it shipped or was scrapped
            self.db.execute("ALTER TABLE items ADD COLUMN finished_at REAL NOT NULL DEFAULT 0")
        # Which Claude model and effort a run used ("" on runs from before this was recorded,
        # and on runs that started no Claude).
        have_runs = {r["name"] for r in self.db.execute("PRAGMA table_info(runs)")}
        for col in ("model", "effort", "agent"):  # agent: claude, gemini, antigravity, copilot
            if col not in have_runs:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        if "tokens" not in have_runs:  # what the agent reported using (the others report no cost)
            self.db.execute("ALTER TABLE runs ADD COLUMN tokens INTEGER NOT NULL DEFAULT 0")
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

    def list_items(self, log_tail: int | None = None) -> list[dict]:
        """Every crate. With log_tail, only the last lines of each log (log_lines says how many
        there are): the game asks for a crate's whole log when you open it."""
        rows = self.db.execute("SELECT * FROM items ORDER BY id").fetchall()
        items = [self._row(r) for r in rows]
        if log_tail is not None:
            for item in items:
                item["log_lines"] = len(item["log"])
                item["log"] = item["log"][-log_tail:] if log_tail else []
        return items

    def update_item(self, item_id: int, **fields) -> None:
        if fields.get("state") in TERMINAL_STATES and "finished_at" not in fields:
            fields["finished_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE items SET {cols} WHERE id = ?", (*fields.values(), item_id))
        self.db.commit()

    def append_log(self, item_id: int, line: str) -> None:
        row = self.db.execute("SELECT log FROM items WHERE id = ?", (item_id,)).fetchone()
        log = json.loads(row["log"])
        log.append(line)
        self.update_item(item_id, log=json.dumps(log[-MAX_LOG_LINES:]))

    # Machine runs: how long each took, what Claude cost, how it ended ------------------
    def record_run(self, item_id: int, pipeline: str, machine: str, machine_type: str, started_at: float,
                   outcome: str, failed_step: str = "", cost_usd: float = 0.0, turns: int = 0,
                   model: str = "", effort: str = "", agent: str = "", tokens: int = 0) -> None:
        """outcome: "ok", "failed" or "stopped" (the backend stopped mid-run). agent/model/effort: what
        its agent steps ran with (agent: claude, gemini, antigravity or copilot; "default" = the CLI's
        own), "" when it ran no agent. tokens: what the agent reported (Claude also reports cost)."""
        self.db.execute(
            "INSERT INTO runs (item_id, pipeline, machine, machine_type, started_at, ended_at, outcome,"
            " failed_step, cost_usd, turns, model, effort, agent, tokens)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_id, pipeline, machine, machine_type, started_at, time.time(), outcome, failed_step,
             float(cost_usd), int(turns), str(model or ""), str(effort or ""), str(agent or ""), int(tokens or 0)))
        self.db.commit()

    def runs_since(self, since: float) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM runs WHERE ended_at >= ? ORDER BY id", (since,))]

    def spent_since(self, since: float) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM runs WHERE ended_at >= ?", (since,)).fetchone()
        return float(row["c"])

    def item_costs(self) -> dict[int, tuple[float, float]]:
        """{crate: (Claude cost in USD, seconds spent in machines)}."""
        return {r["item_id"]: (float(r["c"]), float(r["t"])) for r in self.db.execute(
            "SELECT item_id, SUM(cost_usd) AS c, SUM(ended_at - started_at) AS t FROM runs GROUP BY item_id")}

    def item_cost(self, item_id: int) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM runs WHERE item_id = ?", (item_id,)).fetchone()
        return float(row["c"])

    def finished_since(self, since: float) -> list[dict]:
        """Crates shipped or scrapped since then (without their logs)."""
        return [dict(r) for r in self.db.execute(
            "SELECT id, state, pipeline, created_at, finished_at, loops, scrap_kind FROM items"
            " WHERE finished_at >= ? AND state IN ('shipped', 'scrapped') ORDER BY id", (since,))]

    def count_on_line(self, pipeline: str, states: tuple) -> int:
        row = self.db.execute(
            f"SELECT COUNT(*) AS n FROM items WHERE pipeline = ? AND state IN ({','.join('?' * len(states))})",
            (pipeline, *states)).fetchone()
        return int(row["n"])

    def at_intake(self, pipeline: str, storage: str) -> int:
        """Crates out of a storage still waiting at their line's intake (not on a belt yet)."""
        row = self.db.execute("SELECT COUNT(*) AS n FROM items WHERE pipeline = ? AND storage = ? AND state = 'queued'",
                              (pipeline, storage)).fetchone()
        return int(row["n"])

    def in_flight(self) -> list[dict]:
        """Crates a previous run left on a belt or in a machine."""
        rows = self.db.execute(
            f"SELECT id FROM items WHERE state IN ({','.join('?' * len(IN_FLIGHT_STATES))}) ORDER BY id",
            IN_FLIGHT_STATES).fetchall()
        return [self.get_item(r["id"]) for r in rows]

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

    def retries(self, source: str, ref: str) -> tuple[int, str]:
        """How many times a scrap bin has sent this issue back to be tried again, and why the
        last try failed (the note the next crate's spec carries)."""
        row = self.db.execute("SELECT count, note FROM intake_retries WHERE source = ? AND ref = ?",
                              (source, ref)).fetchone()
        return (int(row["count"]), str(row["note"])) if row is not None else (0, "")

    def add_retry(self, source: str, ref: str, note: str) -> int:
        """Count one more retry of this issue. Returns the new count."""
        count = self.retries(source, ref)[0] + 1
        self.db.execute("INSERT OR REPLACE INTO intake_retries (source, ref, count, note) VALUES (?, ?, ?, ?)",
                        (source, ref, count, note[:4000]))
        self.db.commit()
        return count

    def clear_retries(self, source: str, ref: str) -> None:
        self.db.execute("DELETE FROM intake_retries WHERE source = ? AND ref = ?", (source, ref))
        self.db.commit()

    # Custom storage queues ---------------------------------------------------
    # Changes typed into a Custom storage wait here, in order, until its line takes the next one.
    def queue_list(self, pipeline: str, storage: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM custom_queue WHERE pipeline = ? AND storage = ? ORDER BY pos, id",
                               (pipeline, storage)).fetchall()
        return [dict(r) for r in rows]

    def queue_keys(self) -> list[tuple[str, str]]:
        """Every (pipeline, storage) with something queued."""
        rows = self.db.execute("SELECT DISTINCT pipeline, storage FROM custom_queue").fetchall()
        return [(r["pipeline"], r["storage"]) for r in rows]

    def queue_get(self, entry_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM custom_queue WHERE id = ?", (entry_id,)).fetchone()
        return dict(row) if row is not None else None

    def queue_add(self, pipeline: str, storage: str, text: str) -> dict:
        row = self.db.execute("SELECT MAX(pos) AS p FROM custom_queue WHERE pipeline = ? AND storage = ?",
                              (pipeline, storage)).fetchone()
        pos = (row["p"] if row["p"] is not None else 0) + 1
        cur = self.db.execute("INSERT INTO custom_queue (pipeline, storage, text, pos, created_at) VALUES (?, ?, ?, ?, ?)",
                              (pipeline, storage, text, pos, time.time()))
        self.db.commit()
        return self.queue_get(cur.lastrowid)

    def queue_edit(self, entry_id: int, text: str) -> bool:
        cur = self.db.execute("UPDATE custom_queue SET text = ? WHERE id = ?", (text, entry_id))
        self.db.commit()
        return cur.rowcount > 0

    def queue_remove(self, entry_id: int) -> bool:
        cur = self.db.execute("DELETE FROM custom_queue WHERE id = ?", (entry_id,))
        self.db.commit()
        return cur.rowcount > 0

    def queue_move(self, entry_id: int, delta: int) -> bool:
        """Move an entry up (delta < 0) or down its queue by that many places."""
        entry = self.queue_get(entry_id)
        if entry is None:
            return False
        rows = self.queue_list(entry["pipeline"], entry["storage"])
        ids = [r["id"] for r in rows]
        i = ids.index(entry_id)
        j = max(0, min(len(ids) - 1, i + int(delta)))
        ids.insert(j, ids.pop(i))
        for pos, rid in enumerate(ids, 1):
            self.db.execute("UPDATE custom_queue SET pos = ? WHERE id = ?", (pos, rid))
        self.db.commit()
        return True

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
