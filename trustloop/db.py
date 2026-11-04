"""SQLite storage. One Store per database file; the app uses one, tests and the evaluator use their own."""

import contextvars
import json
import sqlite3
import threading
from contextlib import contextmanager

# Bump when the tables change: older databases are then rebuilt from scratch on start.
SCHEMA_VERSION = 2
TABLES = ("settings", "customers", "cards", "declines", "bank_events", "tool_calls", "outcomes", "transcripts",
          "promises", "messages", "failures", "instructions", "patches", "eval_runs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT UNIQUE NOT NULL,
    last4 TEXT NOT NULL, dob TEXT NOT NULL, email TEXT NOT NULL, address TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS cards (
    customer_id TEXT PRIMARY KEY, card_id TEXT UNIQUE NOT NULL, last4 TEXT NOT NULL, status TEXT NOT NULL,
    international INTEGER NOT NULL DEFAULT 0, international_until TEXT,
    replacement_card_id TEXT, replacement_status TEXT, replacement_requested_at TEXT, replacement_eta TEXT
);
CREATE TABLE IF NOT EXISTS declines (
    id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT NOT NULL, at TEXT NOT NULL,
    merchant TEXT NOT NULL, amount INTEGER NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bank_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, customer_id TEXT NOT NULL,
    kind TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, session_id TEXT NOT NULL,
    customer_id TEXT, tool TEXT NOT NULL, args TEXT NOT NULL, result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, session_id TEXT NOT NULL,
    customer_id TEXT, intent TEXT NOT NULL, resolved INTEGER NOT NULL,
    escalated INTEGER NOT NULL, reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS transcripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, session_id TEXT NOT NULL,
    customer_id TEXT, agent_name TEXT NOT NULL, agent_type TEXT NOT NULL,
    channel TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promises (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, session_id TEXT NOT NULL,
    transcript_id INTEGER, customer_id TEXT, made_by TEXT NOT NULL, agent_type TEXT NOT NULL,
    quote TEXT NOT NULL, kind TEXT NOT NULL, description TEXT NOT NULL,
    due_at TEXT NOT NULL, due_phrase TEXT NOT NULL DEFAULT '', no_date INTEGER NOT NULL DEFAULT 0,
    policy_violation TEXT NOT NULL DEFAULT '', kept_at TEXT, kept_note TEXT NOT NULL DEFAULT '',
    dismissed INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, promise_id INTEGER,
    customer_id TEXT, audience TEXT NOT NULL, purpose TEXT NOT NULL, body TEXT NOT NULL,
    status TEXT NOT NULL, UNIQUE (promise_id, purpose)
);
CREATE TABLE IF NOT EXISTS failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, session_id TEXT NOT NULL,
    customer_id TEXT, failure_type TEXT NOT NULL, intent TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL, expected TEXT NOT NULL, severity TEXT NOT NULL,
    source TEXT NOT NULL, UNIQUE (session_id, failure_type)
);
CREATE TABLE IF NOT EXISTS instructions (
    version INTEGER PRIMARY KEY, at TEXT NOT NULL, label TEXT NOT NULL, text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS patches (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, base_version INTEGER NOT NULL,
    kind TEXT NOT NULL, text TEXT NOT NULL, diff TEXT NOT NULL, changes TEXT NOT NULL,
    source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft'
);
CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, patch_id INTEGER NOT NULL,
    label TEXT NOT NULL, results TEXT NOT NULL, score REAL NOT NULL, source TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        # A shared connection so ":memory:" databases keep their data between calls.
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self._conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            for table in TABLES:
                self._conn.execute(f"DROP TABLE IF EXISTS {table}")
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._conn.executescript(SCHEMA)

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return cursor.lastrowid

    def query(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple | list = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    @contextmanager
    def transaction(self):
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def close(self) -> None:
        self._conn.close()


_current: contextvars.ContextVar[Store | None] = contextvars.ContextVar("trustloop_store", default=None)
_default: Store | None = None


def set_default_store(store: Store) -> None:
    global _default
    _default = store


def get_store() -> Store:
    """The store for the current context: the evaluator's sandbox if one is active, else the app's."""
    store = _current.get() or _default
    if store is None:
        raise RuntimeError("No store configured")
    return store


@contextmanager
def use_store(store: Store):
    token = _current.set(store)
    try:
        yield store
    finally:
        _current.reset(token)


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
