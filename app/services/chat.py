"""Retrieval-augmented, streaming Q&A with saved chat history and grounding verification.

Flow per question:
1. resolve the conversation (create one on the first message) and load recent turns;
2. pick context: all chunks if the library is small ("full"), else hybrid retrieval;
3. stream the Gemini answer (optionally with Google Search grounding for current law);
4. verify citations and quotes against the exact sources that were sent, then save
   both turns (with sources and the grounding report) to SQLite.

Server-Sent Events: ``conversation`` -> ``sources`` -> ``delta``* -> ``done`` | ``error``.
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from app import prompts
from app.config import Settings
from app.db.repositories import ConversationRepository, DocumentRepository
from app.schemas import ChatRequest, ChatTurn, ConversationSummary, SourceRef, WebSource
from app.services.chunking import Chunk
from app.services.documents import StoredDocument
from app.services.grounding import grounding_report
from app.services.llm import ChatMessage, LLMClient, LLMError
from app.services.retrieval import HybridRetriever

logger = logging.getLogger(__name__)

_SHORT_FOLLOW_UP_WORDS = 7
_MAX_WEB_SOURCES = 8
_TITLE_CHARS = 60
_RETRIEVER_CACHE_SIZE = 32


@dataclass(slots=True)
class ChatContext:
    sources: list[SourceRef]
    mode: Literal["full", "retrieval", "none"]


def sse(event: str, data: Any) -> str:
    """Format one Server-Sent Event."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def conversation_title(message: str) -> str:
    """A short, single-line title taken from the first question."""
    title = " ".join(message.split())
    return title if len(title) <= _TITLE_CHARS else title[: _TITLE_CHARS - 3].rstrip() + "..."


def retrieval_query(message: str, history: Sequence[ChatTurn]) -> str:
    """Short follow-ups ("and the deposit?") are expanded with the previous user question."""
    if len(message.split()) >= _SHORT_FOLLOW_UP_WORDS:
        return message
    previous = next((t.content for t in reversed(history) if t.role == "user"), "")
    return f"{previous}\n{message}".strip()


def build_messages(history: Sequence[ChatTurn], final_user_text: str) -> list[ChatMessage]:
    """Convert stored history into a clean alternating user/model conversation."""
    messages: list[ChatMessage] = []
    for turn in history:
        role: Literal["user", "model"] = "user" if turn.role == "user" else "model"
        text = turn.content.strip()
        if not text or (not messages and role == "model"):
            continue
        if messages and messages[-1].role == role:
            messages[-1] = ChatMessage(role, f"{messages[-1].text}\n\n{text}")
        else:
            messages.append(ChatMessage(role, text))
    if messages and messages[-1].role == "user":  # unanswered turn (e.g. a failed request)
        messages.pop()
    messages.append(ChatMessage("user", final_user_text))
    return messages


def _source_refs(chunks: Sequence[Chunk]) -> list[SourceRef]:
    return [
        SourceRef(
            id=f"S{number}",
            doc_id=chunk.doc_id,
            doc_name=chunk.doc_name,
            location=chunk.location,
            section=chunk.section,
            text=chunk.text,
        )
        for number, chunk in enumerate(chunks, start=1)
    ]


class ChatService:
    def __init__(
        self,
        llm: LLMClient,
        documents: DocumentRepository,
        conversations: ConversationRepository,
        settings: Settings,
    ) -> None:
        self._llm = llm
        self._documents = documents
        self._conversations = conversations
        self._settings = settings
        self._retrievers: OrderedDict[tuple[str, ...], HybridRetriever] = OrderedDict()

    def _retriever(self, documents: Sequence[StoredDocument]) -> HybridRetriever:
        """Cached per document set (documents are immutable once stored)."""
        key = tuple(doc.id for doc in documents)
        retriever = self._retrievers.get(key)
        if retriever is None:
            chunks = [chunk for doc in documents for chunk in doc.chunks]
            retriever = HybridRetriever(chunks, _stack_vectors(documents))
            self._retrievers[key] = retriever
            while len(self._retrievers) > _RETRIEVER_CACHE_SIZE:
                self._retrievers.popitem(last=False)
        self._retrievers.move_to_end(key)
        return retriever

    async def build_context(self, session_id: str, request: ChatRequest, history: Sequence[ChatTurn]) -> ChatContext:
        documents = await self._documents.load(session_id, request.document_ids)
        if not documents:
            return ChatContext(sources=[], mode="none")
        chunks = [chunk for doc in documents for chunk in doc.chunks]
        if sum(len(c.text) for c in chunks) <= self._settings.full_context_char_limit:
            return ChatContext(sources=_source_refs(chunks), mode="full")
        selected = await self._retrieve(documents, retrieval_query(request.message, history))
        return ChatContext(sources=_source_refs(selected), mode="retrieval")

    async def _retrieve(self, documents: list[StoredDocument], query: str) -> list[Chunk]:
        retriever = self._retriever(documents)
        query_vector = None
        if retriever.has_vectors:
            try:
                query_vector = (await self._llm.embed([query], "RETRIEVAL_QUERY"))[0]
            except LLMError:
                logger.warning("Query embedding failed; using keyword retrieval only")
        results = retriever.search(
            query, query_vector, top_k=self._settings.retrieval_top_k, diversify=len(documents) > 1
        )
        order = {doc.id: position for position, doc in enumerate(documents)}
        # Present sources in reading order: easier for the model to reason over.
        return sorted((chunk for chunk, _ in results), key=lambda c: (order[c.doc_id], c.index))

    async def _conversation(self, session_id: str, request: ChatRequest) -> ConversationSummary | None:
        if request.conversation_id:
            return await self._conversations.summary(session_id, request.conversation_id)
        return await self._conversations.create(session_id, conversation_title(request.message))

    async def stream(self, session_id: str, request: ChatRequest) -> AsyncIterator[str]:
        try:
            conversation = await self._conversation(session_id, request)
            if conversation is None:
                yield sse("error", {"message": "This chat no longer exists. Please start a new chat."})
                return
            yield sse("conversation", conversation.model_dump())

            history = await self._conversations.recent_turns(conversation.id, self._settings.history_turns)
            await self._conversations.add_message(conversation.id, "user", request.message, {"voice": request.voice})
            context = await self.build_context(session_id, request, history)
            sources = [s.model_dump() for s in context.sources]
            yield sse("sources", {"mode": context.mode, "sources": sources})

            sources_block = prompts.format_sources(
                (s.id, s.doc_name, s.location, s.section, s.text) for s in context.sources
            )
            messages = build_messages(history, prompts.chat_user_turn(request.message, sources_block))
            system = prompts.chat_system_prompt(request, voice=request.voice, web_search=request.web_search)
            budget = self._settings.voice_thinking_budget if request.voice else self._settings.chat_thinking_budget

            answer: list[str] = []
            web_sources: dict[str, WebSource] = {}
            queries: list[str] = []
            async for event in self._llm.stream_chat(
                system=system, messages=messages, web_search=request.web_search, thinking_budget=budget
            ):
                if event.text:
                    answer.append(event.text)
                    yield sse("delta", {"text": event.text})
                for title, uri in event.web_sources:
                    web_sources.setdefault(uri, WebSource(title=title, uri=uri))
                queries.extend(q for q in event.search_queries if q not in queries)

            text = "".join(answer)
            report = grounding_report(text, {s.id: s.text for s in context.sources})
            done: dict[str, Any] = {
                "grounding": report.model_dump(),
                "web_sources": [w.model_dump() for w in list(web_sources.values())[:_MAX_WEB_SOURCES]],
                "search_queries": queries[:5],
            }
            if text.strip():
                meta = {"mode": context.mode, "sources": sources, **done}
                done["message_id"] = await self._conversations.add_message(conversation.id, "assistant", text, meta)
            yield sse("done", done)
        except LLMError as exc:
            yield sse("error", {"message": str(exc)})
        except Exception:  # the HTTP response has started; report instead of crashing the stream
            logger.exception("Chat stream failed")
            yield sse("error", {"message": "Something went wrong while answering. Please try again."})


def _stack_vectors(documents: Sequence[StoredDocument]) -> np.ndarray | None:
    """Stack per-document vectors; documents without embeddings get zero rows."""
    with_vectors = [d.vectors for d in documents if d.vectors is not None]
    if not with_vectors:
        return None
    dim = with_vectors[0].shape[1]
    rows = [d.vectors if d.vectors is not None else np.zeros((len(d.chunks), dim), dtype=np.float32) for d in documents]
    return np.vstack(rows)
