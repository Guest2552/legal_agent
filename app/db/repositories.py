"""Repositories: every SQL statement lives here; the rest of the app uses domain objects.

All queries are parameterised (no string-built SQL with user input) and every read or
write of user data is scoped by ``session_id``, so one browser can never reach another's
documents or chats even if it guesses an id.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from app.db.database import Database
from app.schemas import ChatTurn, ConversationDetail, ConversationSummary, DocumentInfo, MessageOut
from app.services.chunking import Chunk
from app.services.documents import StoredDocument
from app.services.parsers import Segment

TouchResult = Literal["missing", "fresh", "refreshed"]


class CapacityError(Exception):
    """The session's library is full. ``code`` is ``too_many_documents`` or ``library_full``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --------------------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------------------


class SessionRepository:
    """Anonymous browser sessions with sliding expiry."""

    TOUCH_INTERVAL_S = 300  # write last_seen at most every 5 minutes

    def __init__(self, db: Database, ttl_seconds: float, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._ttl = ttl_seconds
        self._clock = clock

    async def touch(self, session_id: str) -> TouchResult:
        now = self._clock()

        def work(conn: sqlite3.Connection) -> TouchResult:
            row = conn.execute("SELECT last_seen FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None or row["last_seen"] < now - self._ttl:
                return "missing"
            if now - row["last_seen"] < self.TOUCH_INTERVAL_S:
                return "fresh"
            conn.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (now, session_id))
            return "refreshed"

        return await self._db.run(work)

    async def create(self, session_id: str) -> None:
        now = self._clock()
        await self._db.run(
            lambda conn: conn.execute(
                "INSERT OR REPLACE INTO sessions (id, created_at, last_seen) VALUES (?, ?, ?)",
                (session_id, now, now),
            )
        )

    async def purge_expired(self) -> int:
        """Delete idle sessions; documents, chats and caches cascade with them."""
        cutoff = self._clock() - self._ttl
        return await self._db.run(
            lambda conn: conn.execute("DELETE FROM sessions WHERE last_seen < ?", (cutoff,)).rowcount
        )


# --------------------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------------------

_INFO_SELECT = (
    "SELECT id, name, kind, size_bytes, characters, chunk_count, locations, semantic_search, notes, created_at "
    "FROM documents "
)


def _row_to_info(row: sqlite3.Row) -> DocumentInfo:
    return DocumentInfo(
        id=row["id"],
        name=row["name"],
        kind=row["kind"],
        size_bytes=row["size_bytes"],
        characters=row["characters"],
        chunks=row["chunk_count"],
        locations=row["locations"],
        semantic_search=bool(row["semantic_search"]),
        notes=json.loads(row["notes"]),
        created_at=row["created_at"],
    )


class DocumentRepository:
    """Documents with their chunks and embeddings. Parsed documents are cached in memory (LRU)."""

    def __init__(self, db: Database, cache_size: int = 32) -> None:
        self._db = db
        self._cache: OrderedDict[str, StoredDocument] = OrderedDict()
        self._cache_size = cache_size

    def _remember(self, document: StoredDocument) -> None:
        self._cache[document.id] = document
        self._cache.move_to_end(document.id)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    async def add(self, session_id: str, doc: StoredDocument, *, max_documents: int, max_chars: int) -> None:
        """Insert atomically, re-checking the session's capacity inside the transaction."""
        segments = json.dumps([[s.location, s.text] for s in doc.segments], ensure_ascii=False)
        vectors = doc.vectors
        chunk_rows = [
            (doc.id, c.index, c.text, c.location, c.section, vectors[i].tobytes() if vectors is not None else None)
            for i, c in enumerate(doc.chunks)
        ]

        def work(conn: sqlite3.Connection) -> None:
            conn.execute("BEGIN IMMEDIATE")  # serialise concurrent uploads for the capacity check
            count, chars = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(characters), 0) FROM documents WHERE session_id = ?", (session_id,)
            ).fetchone()
            if count >= max_documents:
                raise CapacityError("too_many_documents")
            if chars + doc.characters > max_chars:
                raise CapacityError("library_full")
            conn.execute(
                "INSERT INTO documents (id, session_id, name, kind, size_bytes, sha256, characters, locations,"
                " chunk_count, semantic_search, notes, segments, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doc.id, session_id, doc.name, doc.kind, doc.size_bytes, doc.sha256, doc.characters,
                    len(doc.segments), len(doc.chunks), int(vectors is not None), json.dumps(doc.notes),
                    segments, doc.created_at,
                ),
            )  # fmt: skip
            conn.executemany(
                "INSERT INTO chunks (doc_id, idx, text, location, section, embedding) VALUES (?, ?, ?, ?, ?, ?)",
                chunk_rows,
            )

        await self._db.run(work)
        self._remember(doc)

    async def list_infos(self, session_id: str) -> list[DocumentInfo]:
        rows = await self._db.run(
            lambda conn: conn.execute(
                _INFO_SELECT + "WHERE session_id = ? ORDER BY created_at", (session_id,)
            ).fetchall()
        )
        return [_row_to_info(row) for row in rows]

    async def find_by_sha(self, session_id: str, sha256: str) -> DocumentInfo | None:
        row = await self._db.run(
            lambda conn: conn.execute(
                _INFO_SELECT + "WHERE session_id = ? AND sha256 = ?", (session_id, sha256)
            ).fetchone()
        )
        return _row_to_info(row) if row else None

    async def stats(self, session_id: str) -> tuple[int, int]:
        """(document count, total characters) for the session."""
        count, chars = await self._db.run(
            lambda conn: conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(characters), 0) FROM documents WHERE session_id = ?", (session_id,)
            ).fetchone()
        )
        return int(count), int(chars)

    async def load(self, session_id: str, doc_ids: Sequence[str] | None = None) -> list[StoredDocument]:
        """Documents owned by the session in upload order, optionally limited to ``doc_ids``."""

        def owned(conn: sqlite3.Connection) -> list[str]:
            rows = conn.execute(
                "SELECT id FROM documents WHERE session_id = ? ORDER BY created_at", (session_id,)
            ).fetchall()
            return [row["id"] for row in rows]

        ids = await self._db.run(owned)
        if doc_ids:
            wanted = set(doc_ids)
            ids = [doc_id for doc_id in ids if doc_id in wanted]
        missing = [doc_id for doc_id in ids if doc_id not in self._cache]
        if missing:
            for document in await self._db.run(lambda conn: _load_documents(conn, missing)):
                self._remember(document)
        return [self._cache[doc_id] for doc_id in ids if doc_id in self._cache]

    async def get(self, session_id: str, doc_id: str) -> StoredDocument | None:
        documents = await self.load(session_id, [doc_id])
        return documents[0] if documents else None

    async def delete(self, session_id: str, doc_id: str) -> bool:
        deleted = await self._db.run(
            lambda conn: (
                conn.execute("DELETE FROM documents WHERE id = ? AND session_id = ?", (doc_id, session_id)).rowcount
            )
        )
        self._cache.pop(doc_id, None)
        return bool(deleted)

    async def clear(self, session_id: str) -> None:
        def work(conn: sqlite3.Connection) -> list[str]:
            ids = [r["id"] for r in conn.execute("SELECT id FROM documents WHERE session_id = ?", (session_id,))]
            conn.execute("DELETE FROM documents WHERE session_id = ?", (session_id,))
            return ids

        for doc_id in await self._db.run(work):
            self._cache.pop(doc_id, None)


def _load_documents(conn: sqlite3.Connection, doc_ids: Sequence[str]) -> list[StoredDocument]:
    doc_rows = [conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone() for doc_id in doc_ids]
    by_doc = {
        doc_id: conn.execute("SELECT * FROM chunks WHERE doc_id = ? ORDER BY idx", (doc_id,)).fetchall()
        for doc_id in doc_ids
    }

    documents = []
    for row in filter(None, doc_rows):
        rows = by_doc[row["id"]]
        chunks = [
            Chunk(
                id=f"{row['id']}:{c['idx']}", doc_id=row["id"], doc_name=row["name"], index=c["idx"],
                text=c["text"], location=c["location"], section=c["section"],
            )
            for c in rows
        ]  # fmt: skip
        blobs = [c["embedding"] for c in rows]
        vectors = (
            np.vstack([np.frombuffer(b, dtype=np.float32) for b in blobs])
            if blobs and all(b is not None for b in blobs)
            else None
        )
        documents.append(
            StoredDocument(
                id=row["id"],
                name=row["name"],
                kind=row["kind"],
                size_bytes=row["size_bytes"],
                sha256=row["sha256"],
                segments=[Segment(text, location) for location, text in json.loads(row["segments"])],
                chunks=chunks,
                vectors=vectors,
                notes=json.loads(row["notes"]),
                created_at=row["created_at"],
            )
        )
    return documents


# --------------------------------------------------------------------------------------
# Conversations (chat history)
# --------------------------------------------------------------------------------------

_SUMMARY_SQL = (
    "SELECT c.id, c.title, c.created_at, c.updated_at, COUNT(m.id) AS message_count "
    "FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id "
)


def _row_to_summary(row: sqlite3.Row) -> ConversationSummary:
    return ConversationSummary(
        id=row["id"],
        title=row["title"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        message_count=row["message_count"],
    )


class ConversationRepository:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._clock = clock

    async def create(self, session_id: str, title: str) -> ConversationSummary:
        conversation_id = secrets.token_hex(8)
        now = self._clock()
        await self._db.run(
            lambda conn: conn.execute(
                "INSERT INTO conversations (id, session_id, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, session_id, title, now, now),
            )
        )
        return ConversationSummary(id=conversation_id, title=title, created_at=now, updated_at=now, message_count=0)

    async def list_summaries(self, session_id: str, limit: int = 200) -> list[ConversationSummary]:
        rows = await self._db.run(
            lambda conn: conn.execute(
                _SUMMARY_SQL + "WHERE c.session_id = ? GROUP BY c.id ORDER BY c.updated_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        )
        return [_row_to_summary(row) for row in rows]

    async def summary(self, session_id: str, conversation_id: str) -> ConversationSummary | None:
        row = await self._db.run(
            lambda conn: conn.execute(
                _SUMMARY_SQL + "WHERE c.session_id = ? AND c.id = ? GROUP BY c.id", (session_id, conversation_id)
            ).fetchone()
        )
        return _row_to_summary(row) if row else None

    async def get(self, session_id: str, conversation_id: str) -> ConversationDetail | None:
        def work(conn: sqlite3.Connection) -> ConversationDetail | None:
            head = conn.execute(
                "SELECT id, title FROM conversations WHERE id = ? AND session_id = ?", (conversation_id, session_id)
            ).fetchone()
            if head is None:
                return None
            rows = conn.execute(
                "SELECT id, role, content, meta, created_at FROM messages WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
            messages = [
                MessageOut(
                    id=r["id"], role=r["role"], content=r["content"], meta=json.loads(r["meta"]),
                    created_at=r["created_at"],
                )
                for r in rows
            ]  # fmt: skip
            return ConversationDetail(id=head["id"], title=head["title"], messages=messages)

        return await self._db.run(work)

    async def add_message(self, conversation_id: str, role: str, content: str, meta: dict[str, Any]) -> int:
        now = self._clock()

        def work(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "INSERT INTO messages (conversation_id, role, content, meta, created_at) VALUES (?, ?, ?, ?, ?)",
                (conversation_id, role, content, json.dumps(meta, ensure_ascii=False), now),
            )
            conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
            return int(cursor.lastrowid or 0)

        return await self._db.run(work)

    async def recent_turns(self, conversation_id: str, limit: int) -> list[ChatTurn]:
        rows = await self._db.run(
            lambda conn: conn.execute(
                "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        )
        return [ChatTurn(role=row["role"], content=row["content"][:8000]) for row in reversed(rows)]

    async def rename(self, session_id: str, conversation_id: str, title: str) -> bool:
        return bool(
            await self._db.run(
                lambda conn: (
                    conn.execute(
                        "UPDATE conversations SET title = ? WHERE id = ? AND session_id = ?",
                        (title, conversation_id, session_id),
                    ).rowcount
                )
            )
        )

    async def delete(self, session_id: str, conversation_id: str) -> bool:
        return bool(
            await self._db.run(
                lambda conn: (
                    conn.execute(
                        "DELETE FROM conversations WHERE id = ? AND session_id = ?", (conversation_id, session_id)
                    ).rowcount
                )
            )
        )

    async def clear(self, session_id: str) -> None:
        await self._db.run(lambda conn: conn.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,)))


# --------------------------------------------------------------------------------------
# Analysis cache
# --------------------------------------------------------------------------------------


class AnalysisRepository:
    """Per-session cache of structured analysis results (survives restarts)."""

    def __init__(self, db: Database, clock: Callable[[], float] = time.time) -> None:
        self._db = db
        self._clock = clock

    async def get(self, session_id: str, cache_key: str) -> dict[str, Any] | None:
        row = await self._db.run(
            lambda conn: conn.execute(
                "SELECT payload FROM analyses WHERE session_id = ? AND cache_key = ?", (session_id, cache_key)
            ).fetchone()
        )
        return json.loads(row["payload"]) if row else None

    async def put(self, session_id: str, cache_key: str, payload: dict[str, Any]) -> None:
        now = self._clock()
        await self._db.run(
            lambda conn: conn.execute(
                "INSERT OR REPLACE INTO analyses (session_id, cache_key, payload, created_at) VALUES (?, ?, ?, ?)",
                (session_id, cache_key, json.dumps(payload, ensure_ascii=False), now),
            )
        )


# --------------------------------------------------------------------------------------
# Facade
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class Storage:
    """All repositories backed by one SQLite database."""

    db: Database
    sessions: SessionRepository
    documents: DocumentRepository
    conversations: ConversationRepository
    analyses: AnalysisRepository

    @classmethod
    def open(cls, path: str, session_ttl_seconds: float) -> Storage:
        db = Database(path)
        return cls(
            db=db,
            sessions=SessionRepository(db, session_ttl_seconds),
            documents=DocumentRepository(db),
            conversations=ConversationRepository(db),
            analyses=AnalysisRepository(db),
        )
