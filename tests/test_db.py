"""SQLite repositories: round-trips, capacity limits, expiry cascade and caching."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.db.repositories import CapacityError, SessionRepository, Storage
from app.services.chunking import chunk_segments
from app.services.documents import StoredDocument
from app.services.parsers import Segment


def make_document(doc_id: str, text: str = "The deposit is Rs. 50,000.", with_vectors: bool = True) -> StoredDocument:
    segments = [Segment(text, "Page 1"), Segment("Signed at Chennai.", "Page 2")]
    chunks = chunk_segments(doc_id, f"{doc_id}.pdf", segments)
    vectors = np.random.default_rng(1).random((len(chunks), 8), dtype=np.float32) if with_vectors else None
    return StoredDocument(
        id=doc_id, name=f"{doc_id}.pdf", kind="pdf", size_bytes=100, sha256=f"sha-{doc_id}",
        segments=segments, chunks=chunks, vectors=vectors,
    )  # fmt: skip


@pytest.fixture
async def storage(isolated_database: Path) -> Storage:
    storage = Storage.open(str(isolated_database), session_ttl_seconds=3600)
    await storage.sessions.create("s1")
    await storage.sessions.create("s2")
    return storage


async def test_document_round_trip_with_embeddings(storage: Storage) -> None:
    original = make_document("d1")
    await storage.documents.add("s1", original, max_documents=5, max_chars=10_000)
    fresh = Storage.open(storage.db.path, 3600)  # bypass the in-memory cache
    loaded = await fresh.documents.get("s1", "d1")
    assert loaded is not None
    assert loaded.full_text == original.full_text
    assert [c.section for c in loaded.chunks] == [c.section for c in original.chunks]
    assert loaded.vectors is not None
    assert np.array_equal(loaded.vectors, original.vectors)
    info = (await fresh.documents.list_infos("s1"))[0]
    assert (info.locations, info.semantic_search) == (2, True)


async def test_documents_without_embeddings(storage: Storage) -> None:
    await storage.documents.add("s1", make_document("d1", with_vectors=False), max_documents=5, max_chars=10_000)
    loaded = await Storage.open(storage.db.path, 3600).documents.get("s1", "d1")
    assert loaded is not None
    assert loaded.vectors is None


async def test_documents_are_scoped_to_their_session(storage: Storage) -> None:
    await storage.documents.add("s1", make_document("d1"), max_documents=5, max_chars=10_000)
    assert await storage.documents.get("s2", "d1") is None
    assert await storage.documents.load("s2") == []
    assert not await storage.documents.delete("s2", "d1")
    assert await storage.documents.find_by_sha("s1", "sha-d1") is not None


async def test_capacity_limits_are_enforced_inside_the_transaction(storage: Storage) -> None:
    await storage.documents.add("s1", make_document("d1"), max_documents=1, max_chars=10_000)
    with pytest.raises(CapacityError) as too_many:
        await storage.documents.add("s1", make_document("d2"), max_documents=1, max_chars=10_000)
    assert too_many.value.code == "too_many_documents"
    with pytest.raises(CapacityError) as too_big:
        await storage.documents.add("s2", make_document("d3", "x" * 500), max_documents=5, max_chars=100)
    assert too_big.value.code == "library_full"
    assert await storage.documents.stats("s1") == (1, len(make_document("d1").full_text) - 2)


async def test_expired_sessions_are_purged_with_all_their_data(isolated_database: Path) -> None:
    now = [1000.0]
    storage = Storage.open(str(isolated_database), session_ttl_seconds=3600)
    storage.sessions = SessionRepository(storage.db, 3600, clock=lambda: now[0])
    await storage.sessions.create("old")
    await storage.documents.add("old", make_document("d1"), max_documents=5, max_chars=10_000)
    conversation = await storage.conversations.create("old", "Deposit")
    await storage.conversations.add_message(conversation.id, "user", "hi", {})

    assert await storage.sessions.touch("old") == "fresh"
    now[0] += 400
    assert await storage.sessions.touch("old") == "refreshed"  # sliding expiry
    now[0] += 3601
    assert await storage.sessions.touch("old") == "missing"
    assert await storage.sessions.purge_expired() == 1

    fresh = Storage.open(str(isolated_database), 3600)
    assert await fresh.documents.get("old", "d1") is None
    assert await fresh.conversations.get("old", conversation.id) is None


async def test_recent_turns_are_ordered_and_limited(storage: Storage) -> None:
    conversation = await storage.conversations.create("s1", "Chat")
    for index in range(6):
        role = "user" if index % 2 == 0 else "assistant"
        await storage.conversations.add_message(conversation.id, role, f"m{index}", {})
    turns = await storage.conversations.recent_turns(conversation.id, limit=4)
    assert [t.content for t in turns] == ["m2", "m3", "m4", "m5"]


async def test_analysis_cache_is_per_session(storage: Storage) -> None:
    await storage.analyses.put("s1", "key", {"result": {"ok": True}})
    assert await storage.analyses.get("s1", "key") == {"result": {"ok": True}}
    assert await storage.analyses.get("s2", "key") is None
