"""Streaming Q&A (Server-Sent Events) and saved chat history."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from fastapi.responses import StreamingResponse

from app.core.errors import AppError
from app.db.repositories import Storage
from app.dependencies import Services, ai_rate_limit, get_services, get_session_id, get_storage
from app.schemas import ChatRequest, ConversationDetail, ConversationSummary, RenameRequest

router = APIRouter(prefix="/api", tags=["chat"])

SessionDep = Annotated[str, Depends(get_session_id)]
StorageDep = Annotated[Storage, Depends(get_storage)]


def _not_found() -> AppError:
    return AppError(404, "conversation_not_found", "Chat not found. It may have been deleted.")


@router.post(
    "/chat",
    dependencies=[Depends(ai_rate_limit)],
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "conversation, sources, delta*, done"}},
)
async def chat(
    body: ChatRequest,
    session_id: SessionDep,
    services: Annotated[Services, Depends(get_services)],
) -> StreamingResponse:
    return StreamingResponse(
        services.chat.stream(session_id, body),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/conversations", response_model=list[ConversationSummary])
async def list_conversations(session_id: SessionDep, storage: StorageDep) -> list[ConversationSummary]:
    return await storage.conversations.list_summaries(session_id)


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(conversation_id: str, session_id: SessionDep, storage: StorageDep) -> ConversationDetail:
    conversation = await storage.conversations.get(session_id, conversation_id)
    if conversation is None:
        raise _not_found()
    return conversation


@router.patch("/conversations/{conversation_id}", status_code=204)
async def rename_conversation(
    conversation_id: str, body: RenameRequest, session_id: SessionDep, storage: StorageDep
) -> Response:
    if not await storage.conversations.rename(session_id, conversation_id, " ".join(body.title.split())):
        raise _not_found()
    return Response(status_code=204)


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(conversation_id: str, session_id: SessionDep, storage: StorageDep) -> Response:
    if not await storage.conversations.delete(session_id, conversation_id):
        raise _not_found()
    return Response(status_code=204)


@router.delete("/conversations", status_code=204)
async def delete_all_conversations(session_id: SessionDep, storage: StorageDep) -> Response:
    await storage.conversations.clear(session_id)
    return Response(status_code=204)
