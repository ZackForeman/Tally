"""SQLite storage for runs and their progress events, plus the uploaded files on disk.

One file, easy to reset between demo runs: delete ``data/``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

DATA = Path(__file__).resolve().parent.parent / "data"


class Store:
    def __init__(self, root: Path = DATA):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(root / "tally.sqlite", check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, created TEXT NOT NULL, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL, seq INTEGER NOT NULL, t REAL NOT NULL, data TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq));
            """)

    def run_dir(self, run_id: str) -> Path:
        path = self.root / "runs" / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_run(self, run_id: str, created: str, state: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO runs (id, created, state) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET state = excluded.state",
                (run_id, created, state),
            )

    def load_run(self, run_id: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
        return row[0] if row else None

    def list_runs(self) -> list[tuple[str, str]]:
        with self._lock:
            return self._db.execute("SELECT id, created FROM runs ORDER BY created DESC").fetchall()

    def add_event(self, run_id: str, seq: int, t: float, event: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO events (run_id, seq, t, data) VALUES (?, ?, ?, ?)",
                (run_id, seq, t, json.dumps(event, default=str)),
            )

    def events(self, run_id: str) -> list[tuple[float, dict[str, Any]]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT t, data FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
        return [(t, json.loads(d)) for t, d in rows]
