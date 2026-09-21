"""Upload pipeline: validate -> parse -> chunk -> embed -> persist."""

from __future__ import annotations

import hashlib
import logging
import secrets

from app.config import Settings
from app.core.errors import AppError
from app.db.repositories import CapacityError, DocumentRepository
from app.schemas import DocumentInfo
from app.services.chunking import chunk_segments
from app.services.documents import StoredDocument
from app.services.llm import LLMClient, LLMError
from app.services.parsers import DocumentError, ParsedDocument, parse_document, safe_filename

logger = logging.getLogger(__name__)


class IngestionService:
    def __init__(self, llm: LLMClient, documents: DocumentRepository, settings: Settings) -> None:
        self._llm = llm
        self._documents = documents
        self._settings = settings

    def _capacity_error(self, code: str) -> AppError:
        if code == "too_many_documents":
            limit = self._settings.max_documents_per_session
            return AppError(409, code, f"You can keep up to {limit} documents. Remove one first.")
        return AppError(413, code, "Your document library is full. Remove a document first.")

    async def ingest_file(self, session_id: str, filename: str | None, data: bytes) -> DocumentInfo:
        name = safe_filename(filename)
        digest = hashlib.sha256(data).hexdigest()
        duplicate = await self._documents.find_by_sha(session_id, digest)
        if duplicate is not None:
            return duplicate
        count, _ = await self._documents.stats(session_id)
        if count >= self._settings.max_documents_per_session:  # fail fast before the expensive parse
            raise self._capacity_error("too_many_documents")
        try:
            parsed = await parse_document(name, data, self._llm)
        except DocumentError as exc:
            raise AppError(415, "unreadable_document", str(exc)) from exc
        except LLMError as exc:
            raise AppError(502, "ai_unavailable", str(exc)) from exc
        document = await self._build(name, parsed, len(data), digest)
        try:
            await self._documents.add(
                session_id,
                document,
                max_documents=self._settings.max_documents_per_session,
                max_chars=self._settings.max_session_chars,
            )
        except CapacityError as exc:
            raise self._capacity_error(exc.code) from exc
        return document.info()

    async def ingest_text(self, session_id: str, name: str, text: str) -> DocumentInfo:
        filename = safe_filename(name, default="Pasted text")
        if not filename.lower().endswith((".txt", ".md")):
            filename = f"{filename}.txt"
        return await self.ingest_file(session_id, filename, text.encode("utf-8"))

    async def _build(self, name: str, parsed: ParsedDocument, size: int, digest: str) -> StoredDocument:
        doc_id = secrets.token_hex(8)
        chunks = chunk_segments(doc_id, name, parsed.segments, self._settings.chunk_size, self._settings.chunk_overlap)
        notes = list(parsed.notes)
        try:
            vectors = await self._llm.embed([c.embedding_text for c in chunks], "RETRIEVAL_DOCUMENT")
        except LLMError:
            logger.warning("Embedding failed; document %s will use keyword search only", doc_id)
            vectors = None
            notes.append("Semantic search unavailable right now; keyword search is used.")
        return StoredDocument(
            id=doc_id,
            name=name,
            kind=parsed.kind,
            size_bytes=size,
            sha256=digest,
            segments=parsed.segments,
            chunks=chunks,
            vectors=vectors,
            notes=notes,
        )
