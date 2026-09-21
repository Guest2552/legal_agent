"""LexiGuide application factory.

Run locally with ``uvicorn app.main:app --reload`` and open http://localhost:8000.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import analysis, chat, documents, misc
from app.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.rate_limit import RateLimiter
from app.core.security import (
    BodySizeLimitMiddleware,
    OriginCheckMiddleware,
    SecurityHeadersMiddleware,
    SessionMiddleware,
)
from app.db.repositories import Storage
from app.dependencies import Services
from app.services.llm import GeminiClient, LLMClient, LLMError

STATIC_DIR = Path(__file__).parent / "static"
_MULTIPART_OVERHEAD = 1024 * 1024

logger = logging.getLogger("lexiguide")


def _build_services(settings: Settings, storage: Storage, llm: LLMClient | None) -> Services | None:
    if llm is None:
        try:
            llm = GeminiClient(settings)
        except LLMError:
            logger.error("GEMINI_API_KEY is missing: AI features are disabled until it is set.")
            return None
    return Services.build(llm, storage, settings)


def create_app(settings: Settings | None = None, llm: LLMClient | None = None) -> FastAPI:
    """Build the FastAPI app. ``llm`` can be injected (tests use a fake client)."""
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    docs_enabled = settings.enable_api_docs
    app = FastAPI(
        title="LexiGuide API",
        version="1.0.0",
        summary="Grounded legal-document assistant powered by Gemini.",
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    storage = Storage.open(settings.database_path, settings.session_ttl_seconds)
    app.state.settings = settings
    app.state.storage = storage
    app.state.rate_limiter = RateLimiter(limit=settings.ai_requests_per_minute, window=60)
    app.state.services = _build_services(settings, storage, llm)

    register_exception_handlers(app)
    for router in (documents.router, chat.router, analysis.router, misc.router):
        app.include_router(router)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "img" / "favicon.svg", media_type="image/svg+xml")

    # Order: the last added middleware runs first (outermost).
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        SessionMiddleware,
        sessions=storage.sessions,
        max_age=settings.session_ttl_seconds,
        secure=settings.cookie_secure,
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_upload_bytes + _MULTIPART_OVERHEAD)
    app.add_middleware(OriginCheckMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    return app


app = create_app()
