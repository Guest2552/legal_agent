"""Voice transcription, export, health and client configuration endpoints."""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Response, UploadFile

from app.api.documents import read_upload
from app.config import Settings
from app.core.errors import AppError
from app.dependencies import Services, ai_rate_limit, get_services, get_settings_dep
from app.schemas import JURISDICTIONS, LANGUAGES, ExportRequest
from app.services.export import markdown_to_docx
from app.services.llm import LLMError
from app.services.parsers import ACCEPTED_EXTENSIONS, MAX_PDF_PAGES, DocumentError, detect_file_type

router = APIRouter(prefix="/api", tags=["utilities"])
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
_MAX_VOICE_BYTES = 10 * 1024 * 1024
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/config")
async def client_config(settings: SettingsDep) -> dict[str, Any]:
    """Single source of truth for the UI's selectors and limits."""
    return {
        "languages": LANGUAGES,
        "jurisdictions": list(JURISDICTIONS),
        "accepted_extensions": list(ACCEPTED_EXTENSIONS),
        "max_upload_mb": settings.max_upload_mb,
        "max_documents": settings.max_documents_per_session,
        "session_ttl_days": settings.session_ttl_days,
        "max_pdf_pages": MAX_PDF_PAGES,
        "chat_model": settings.gemini_chat_model,
    }


@router.post("/voice/transcribe", dependencies=[Depends(ai_rate_limit)])
async def transcribe(
    services: Annotated[Services, Depends(get_services)],
    audio: Annotated[UploadFile, File(description="Short speech recording (WAV, MP3, OGG, WebM, M4A)")],
) -> dict[str, str]:
    data = await read_upload(audio, _MAX_VOICE_BYTES)
    try:
        file_type = detect_file_type(audio.filename or "voice.wav", data)
    except DocumentError as exc:
        raise AppError(415, "unsupported_audio", str(exc)) from exc
    if file_type.kind != "audio":
        raise AppError(415, "unsupported_audio", "Please send an audio recording.")
    try:
        text = await services.llm.transcribe(data, file_type.mime)
    except LLMError as exc:
        raise AppError(502, "ai_unavailable", str(exc)) from exc
    return {"text": text}


@router.post("/export/docx")
async def export_docx(body: ExportRequest) -> Response:
    filename = re.sub(r"[^A-Za-z0-9 _-]+", "", body.title).strip().replace(" ", "_")[:60] or "LexiGuide"
    return Response(
        content=markdown_to_docx(body.title, body.markdown),
        media_type=_DOCX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}.docx"'},
    )
