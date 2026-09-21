"""SQLite connection handling and schema.

Uses the standard-library ``sqlite3`` driver (no extra dependency). Each unit of work gets
its own short-lived connection inside a transaction and runs in a worker thread, so the
event loop never blocks on disk I/O. WAL mode lets readers and a writer work concurrently.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,          -- SHA-256 of the cookie token, never the token itself
    created_at  REAL NOT NULL,
    last_seen   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id               TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    kind             TEXT NOT NULL,
    size_bytes       INTEGER NOT NULL,
    sha256           TEXT NOT NULL,
    characters       INTEGER NOT NULL,
    locations        INTEGER NOT NULL,
    chunk_count      INTEGER NOT NULL,
    semantic_search  INTEGER NOT NULL,
    notes            TEXT NOT NULL,        -- JSON list
    segments         TEXT NOT NULL,        -- JSON list of [location, text]
    created_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_session ON documents(session_id, created_at);

CREATE TABLE IF NOT EXISTS chunks (
    doc_id     TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    text       TEXT NOT NULL,
    location   TEXT NOT NULL,
    section    TEXT NOT NULL,
    embedding  BLOB,                       -- float32 vector, NULL when unavailable
    PRIMARY KEY (doc_id, idx)
);

CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_session ON conversations(session_id, updated_at);

CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role             TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content          TEXT NOT NULL,
    meta             TEXT NOT NULL,        -- JSON: sources, grounding, web sources
    created_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS analyses (
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    cache_key   TEXT NOT NULL,
    payload     TEXT NOT NULL,             -- JSON
    created_at  REAL NOT NULL,
    PRIMARY KEY (session_id, cache_key)
);
"""


class Database:
    """Thin wrapper that runs units of work against one SQLite file."""

    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def run_sync(self, work: Callable[[sqlite3.Connection], T]) -> T:
        """Run ``work`` in one transaction (committed on success, rolled back on error)."""
        with closing(self._connect()) as conn, conn:
            return work(conn)

    async def run(self, work: Callable[[sqlite3.Connection], T]) -> T:
        """Async version of :meth:`run_sync`, executed in a worker thread."""
        return await asyncio.to_thread(self.run_sync, work)
