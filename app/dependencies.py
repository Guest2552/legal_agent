"""FastAPI dependencies: app-wide services, storage, the caller's session and rate limiting."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from app.config import Settings
from app.core.errors import AppError
from app.core.rate_limit import RateLimiter
from app.db.repositories import Storage
from app.services.analysis import AnalysisService
from app.services.chat import ChatService
from app.services.documents import StoredDocument
from app.services.ingestion import IngestionService
from app.services.llm import LLMClient


@dataclass(slots=True)
class Services:
    llm: LLMClient
    ingestion: IngestionService
    analysis: AnalysisService
    chat: ChatService

    @classmethod
    def build(cls, llm: LLMClient, storage: Storage, settings: Settings) -> Services:
        return cls(
            llm=llm,
            ingestion=IngestionService(llm, storage.documents, settings),
            analysis=AnalysisService(llm, storage.analyses, settings),
            chat=ChatService(llm, storage.documents, storage.conversations, settings),
        )


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_storage(request: Request) -> Storage:
    storage: Storage = request.app.state.storage
    return storage


def get_services(request: Request) -> Services:
    services: Services | None = request.app.state.services
    if services is None:
        raise AppError(503, "ai_not_configured", "The AI service is not configured on the server.")
    return services


def get_session_id(request: Request) -> str:
    """Opaque id of the caller's session (set by SessionMiddleware)."""
    session_id: str = request.state.session_id
    return session_id


async def load_document(storage: Storage, session_id: str, doc_id: str) -> StoredDocument:
    document = await storage.documents.get(session_id, doc_id)
    if document is None:
        raise AppError(404, "document_not_found", "Document not found. It may have been deleted.")
    return document


def ai_rate_limit(request: Request) -> None:
    """Throttle AI-backed endpoints per client IP."""
    limiter: RateLimiter = request.app.state.rate_limiter
    client = request.client.host if request.client else "unknown"
    retry_after = limiter.hit(client)
    if retry_after is not None:
        raise AppError(
            429,
            "rate_limited",
            f"Too many requests. Please wait {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )
