"""Structured analysis endpoints (simplify, risk scan, compare, action plan, lawyer brief)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.core.errors import AppError
from app.db.repositories import Storage
from app.dependencies import Services, ai_rate_limit, get_services, get_session_id, get_storage, load_document
from app.schemas import CompareRequest, RiskRequest, SimplifyRequest, SituationRequest
from app.services.documents import StoredDocument
from app.services.llm import LLMError

router = APIRouter(prefix="/api/analysis", tags=["analysis"], dependencies=[Depends(ai_rate_limit)])

SessionDep = Annotated[str, Depends(get_session_id)]
StorageDep = Annotated[Storage, Depends(get_storage)]
ServicesDep = Annotated[Services, Depends(get_services)]
Payload = dict[str, Any]


def _ai_error(exc: LLMError) -> AppError:
    return AppError(502, "ai_unavailable", str(exc))


async def _selected_documents(storage: Storage, session_id: str, doc_ids: list[str]) -> list[StoredDocument]:
    if not doc_ids:
        return await storage.documents.load(session_id)
    return [await load_document(storage, session_id, doc_id) for doc_id in doc_ids]


@router.post("/simplify")
async def simplify(
    body: SimplifyRequest, session_id: SessionDep, storage: StorageDep, services: ServicesDep
) -> Payload:
    document = await load_document(storage, session_id, body.document_id)
    try:
        return await services.analysis.simplify(session_id, document, body, body.level)
    except LLMError as exc:
        raise _ai_error(exc) from exc


@router.post("/risks")
async def risks(body: RiskRequest, session_id: SessionDep, storage: StorageDep, services: ServicesDep) -> Payload:
    document = await load_document(storage, session_id, body.document_id)
    try:
        return await services.analysis.risks(session_id, document, body)
    except LLMError as exc:
        raise _ai_error(exc) from exc


@router.post("/compare")
async def compare(body: CompareRequest, session_id: SessionDep, storage: StorageDep, services: ServicesDep) -> Payload:
    if body.document_a == body.document_b:
        raise AppError(422, "same_document", "Choose two different documents to compare.")
    doc_a = await load_document(storage, session_id, body.document_a)
    doc_b = await load_document(storage, session_id, body.document_b)
    try:
        return await services.analysis.compare(session_id, doc_a, doc_b, body)
    except LLMError as exc:
        raise _ai_error(exc) from exc


def _require_input(body: SituationRequest, documents: list[StoredDocument]) -> None:
    if not body.situation.strip() and not documents:
        raise AppError(422, "missing_input", "Describe your situation or upload a document first.")


@router.post("/action-plan")
async def action_plan(
    body: SituationRequest, session_id: SessionDep, storage: StorageDep, services: ServicesDep
) -> Payload:
    documents = await _selected_documents(storage, session_id, body.document_ids)
    _require_input(body, documents)
    try:
        return await services.analysis.action_plan(session_id, documents, body, body.situation)
    except LLMError as exc:
        raise _ai_error(exc) from exc


@router.post("/lawyer-brief")
async def lawyer_brief(
    body: SituationRequest, session_id: SessionDep, storage: StorageDep, services: ServicesDep
) -> Payload:
    documents = await _selected_documents(storage, session_id, body.document_ids)
    _require_input(body, documents)
    try:
        return await services.analysis.lawyer_brief(session_id, documents, body, body.situation)
    except LLMError as exc:
        raise _ai_error(exc) from exc
