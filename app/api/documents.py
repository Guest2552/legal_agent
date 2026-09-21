"""Document library endpoints: upload, paste, list, read and delete."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Response, UploadFile

from app.config import Settings
from app.core.errors import AppError
from app.db.repositories import Storage
from app.dependencies import (
    Services,
    ai_rate_limit,
    get_services,
    get_session_id,
    get_settings_dep,
    get_storage,
    load_document,
)
from app.schemas import DocumentInfo, DocumentText, PasteTextRequest

router = APIRouter(prefix="/api/documents", tags=["documents"])

SessionDep = Annotated[str, Depends(get_session_id)]
StorageDep = Annotated[Storage, Depends(get_storage)]
ServicesDep = Annotated[Services, Depends(get_services)]
_PREVIEW_LIMIT = 200_000


async def read_upload(file: UploadFile, max_bytes: int) -> bytes:
    """Read an upload, refusing anything above ``max_bytes``."""
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise AppError(413, "file_too_large", f"Files are limited to {max_bytes // (1024 * 1024)} MB.")
    return data


@router.get("", response_model=list[DocumentInfo])
async def list_documents(session_id: SessionDep, storage: StorageDep) -> list[DocumentInfo]:
    return await storage.documents.list_infos(session_id)


@router.post("", response_model=DocumentInfo, status_code=201, dependencies=[Depends(ai_rate_limit)])
async def upload_document(
    session_id: SessionDep,
    services: ServicesDep,
    settings: Annotated[Settings, Depends(get_settings_dep)],
    file: Annotated[UploadFile, File(description="PDF, Word, Excel, CSV, PowerPoint, text, image or audio")],
) -> DocumentInfo:
    data = await read_upload(file, settings.max_upload_bytes)
    return await services.ingestion.ingest_file(session_id, file.filename, data)


@router.post("/text", response_model=DocumentInfo, status_code=201, dependencies=[Depends(ai_rate_limit)])
async def paste_text(body: PasteTextRequest, session_id: SessionDep, services: ServicesDep) -> DocumentInfo:
    return await services.ingestion.ingest_text(session_id, body.name, body.text)


@router.get("/{doc_id}/text", response_model=DocumentText)
async def document_text(doc_id: str, session_id: SessionDep, storage: StorageDep) -> DocumentText:
    document = await load_document(storage, session_id, doc_id)
    sections: list[dict[str, str]] = []
    used = 0
    for segment in document.segments:
        if used + len(segment.text) > _PREVIEW_LIMIT:
            return DocumentText(id=document.id, name=document.name, sections=sections, truncated=True)
        sections.append({"location": segment.location, "text": segment.text})
        used += len(segment.text)
    return DocumentText(id=document.id, name=document.name, sections=sections, truncated=False)


@router.delete("/{doc_id}", status_code=204)
async def delete_document(doc_id: str, session_id: SessionDep, storage: StorageDep) -> Response:
    if not await storage.documents.delete(session_id, doc_id):
        raise AppError(404, "document_not_found", "Document not found.")
    return Response(status_code=204)


@router.delete("", status_code=204)
async def delete_all_documents(session_id: SessionDep, storage: StorageDep) -> Response:
    await storage.documents.clear(session_id)
    return Response(status_code=204)
