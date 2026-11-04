"""SQLite persistence for SREMS operational and audit records."""

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any


class StorageError(RuntimeError):
    """Raised when a required persistent operation cannot be completed."""


class SQLiteStore:
    def __init__(self, path: str = ":memory:") -> None:
        self._lock = threading.RLock()
        try:
            self._connection = sqlite3.connect(
                path, check_same_thread=False, isolation_level=None
            )
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    category TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    details TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_entries (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    details TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_anchor (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    sequence INTEGER NOT NULL,
                    entry_hash TEXT NOT NULL
                );
                """
            )
            self._connection.execute(
                "INSERT OR IGNORE INTO audit_anchor(id, sequence, entry_hash) VALUES (1, 0, '')"
            )
        except sqlite3.Error as exc:
            raise StorageError(f"Unable to initialize the SREMS store: {exc}") from exc

    def record_event(
        self, category: str, event_type: str, details: dict[str, Any], at: datetime
    ) -> int:
        try:
            with self._lock:
                cursor = self._connection.execute(
                    "INSERT INTO events(timestamp, category, event_type, details) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        at.isoformat(),
                        category,
                        event_type,
                        json.dumps(details, sort_keys=True, default=str),
                    ),
                )
                return int(cursor.lastrowid)
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise StorageError(f"Unable to persist event {event_type}: {exc}") from exc

    def append_audit(
        self,
        actor: str,
        action: str,
        outcome: str,
        details: dict[str, Any],
        at: datetime,
    ) -> int:
        from .audit import audit_hash

        try:
            with self._lock:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    "SELECT sequence, entry_hash FROM audit_entries "
                    "ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                sequence = 1 if row is None else int(row[0]) + 1
                previous_hash = "" if row is None else str(row[1])
                serialized_details = json.dumps(
                    details, sort_keys=True, separators=(",", ":"), default=str
                )
                timestamp = at.isoformat()
                entry_hash = audit_hash(
                    sequence,
                    timestamp,
                    actor,
                    action,
                    outcome,
                    serialized_details,
                    previous_hash,
                )
                self._connection.execute(
                    "INSERT INTO audit_entries "
                    "(sequence, timestamp, actor, action, outcome, details, "
                    "previous_hash, entry_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        sequence,
                        timestamp,
                        actor,
                        action,
                        outcome,
                        serialized_details,
                        previous_hash,
                        entry_hash,
                    ),
                )
                self._connection.execute(
                    "UPDATE audit_anchor SET sequence = ?, entry_hash = ? WHERE id = 1",
                    (sequence, entry_hash),
                )
                self._connection.execute("COMMIT")
                return sequence
        except (sqlite3.Error, TypeError, ValueError) as exc:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise StorageError(f"Unable to persist audit record {action}: {exc}") from exc

    def audit_rows(self) -> list[tuple[Any, ...]]:
        try:
            with self._lock:
                return list(
                    self._connection.execute(
                        "SELECT sequence, timestamp, actor, action, outcome, details, "
                        "previous_hash, entry_hash FROM audit_entries ORDER BY sequence"
                    )
                )
        except sqlite3.Error as exc:
            raise StorageError(f"Unable to read audit records: {exc}") from exc

    def audit_anchor(self) -> tuple[int, str]:
        try:
            with self._lock:
                row = self._connection.execute(
                    "SELECT sequence, entry_hash FROM audit_anchor WHERE id = 1"
                ).fetchone()
                if row is None:
                    raise StorageError("Audit integrity anchor is missing.")
                return int(row[0]), str(row[1])
        except sqlite3.Error as exc:
            raise StorageError(f"Unable to read audit integrity anchor: {exc}") from exc

    def events(
        self, category: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("limit must be positive")
        try:
            with self._lock:
                if category is None:
                    rows = self._connection.execute(
                        "SELECT id, timestamp, category, event_type, details "
                        "FROM events ORDER BY id DESC LIMIT ?",
                        (limit,),
                    )
                else:
                    rows = self._connection.execute(
                        "SELECT id, timestamp, category, event_type, details FROM events "
                        "WHERE category = ? ORDER BY id DESC LIMIT ?",
                        (category, limit),
                    )
                return [
                    {
                        "id": row[0],
                        "timestamp": row[1],
                        "category": row[2],
                        "event_type": row[3],
                        "details": json.loads(row[4]),
                    }
                    for row in rows
                ]
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            raise StorageError(f"Unable to read event history: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            self._connection.close()
