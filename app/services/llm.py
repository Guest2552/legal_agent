"""Gemini access layer.

:class:`LLMClient` is the protocol the rest of the app depends on; :class:`GeminiClient`
implements it with the official ``google-genai`` SDK. Tests inject a fake client, so no
test ever touches the network.

Resilience: every call walks a chain of models (primary, then fallbacks). A model that
answers 429 (quota exhausted) is put on cooldown for the delay Gemini asks for, so later
requests skip it instead of paying for a doomed round trip. Chat degrades gracefully:
if no model can use Google Search grounding, it still answers from the documents.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypeVar

import httpx
import numpy as np
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.prompts import TRANSCRIBE_PROMPT

logger = logging.getLogger(__name__)

EmbedTask = Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]
ModelT = TypeVar("ModelT", bound=BaseModel)
ResultT = TypeVar("ResultT")

_RETRYABLE_CODES = frozenset({429, 500, 502, 503, 504})
_DEFAULT_COOLDOWN_S = 60.0
_MAX_COOLDOWN_S = 3600.0
_RETRY_DELAY = re.compile(r"'retryDelay': '(\d+(?:\.\d+)?)s'")
_EMBED_BATCH_SIZE = 100  # API limit per batchEmbedContents request
_EMBED_CONCURRENCY = 4
_NO_AFC = types.AutomaticFunctionCallingConfig(disable=True)


class LLMError(RuntimeError):
    """A model call failed. The message is safe to show to end users."""


@dataclass(slots=True)
class ChatMessage:
    role: Literal["user", "model"]
    text: str


@dataclass(slots=True)
class StreamEvent:
    """One increment of a streamed answer (text and/or final grounding metadata)."""

    text: str = ""
    web_sources: list[tuple[str, str]] = field(default_factory=list)  # (title, uri)
    search_queries: list[str] = field(default_factory=list)


class LLMClient(Protocol):
    """Everything the application needs from a generative model provider."""

    async def embed(self, texts: Sequence[str], task: EmbedTask) -> np.ndarray: ...

    async def generate_json(
        self, *, system: str, prompt: str, schema: type[ModelT], thinking_budget: int | None = None
    ) -> ModelT: ...

    def stream_chat(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        web_search: bool,
        thinking_budget: int,
    ) -> AsyncIterator[StreamEvent]: ...

    async def transcribe(self, data: bytes, mime_type: str) -> str: ...

    async def extract_text(self, data: bytes, mime_type: str, instruction: str) -> str: ...


class _LRUCache:
    """Small bounded cache for embedding vectors keyed by content hash."""

    def __init__(self, max_items: int) -> None:
        self._items: OrderedDict[str, np.ndarray] = OrderedDict()
        self._max = max_items

    def get(self, key: str) -> np.ndarray | None:
        value = self._items.get(key)
        if value is not None:
            self._items.move_to_end(key)
        return value

    def put(self, key: str, value: np.ndarray) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self._max:
            self._items.popitem(last=False)


class ModelRouter:
    """Orders candidate models and remembers which ones are cooling down after a 429."""

    def __init__(self, fallbacks: Sequence[str], clock: Callable[[], float] = time.monotonic) -> None:
        self._fallbacks = list(fallbacks)
        self._clock = clock
        self._cooldown_until: dict[str, float] = {}

    def candidates(self, primary: str, variant: str = "") -> list[str]:
        """Primary first, then fallbacks, skipping models on cooldown (never returns empty)."""
        ordered = list(dict.fromkeys([primary, *self._fallbacks]))
        now = self._clock()
        ready = [m for m in ordered if self._cooldown_until.get(m + variant, 0.0) <= now]
        return ready or ordered[:1]

    def cool_down(self, model: str, seconds: float, variant: str = "") -> None:
        self._cooldown_until[model + variant] = self._clock() + seconds


def _retry_after(exc: genai_errors.APIError) -> float:
    """Seconds Gemini asks us to wait (RetryInfo.retryDelay), bounded; default one minute."""
    match = _RETRY_DELAY.search(str(exc.details))
    seconds = float(match.group(1)) if match else _DEFAULT_COOLDOWN_S
    return min(max(seconds, 5.0), _MAX_COOLDOWN_S)


_TRANSIENT_NETWORK_ERRORS = (httpx.TimeoutException, httpx.NetworkError)


def _as_api_error(exc: Exception) -> genai_errors.APIError:
    """Treat a hung or dropped connection like a gateway timeout, so the next model is tried."""
    if isinstance(exc, genai_errors.APIError):
        return exc
    return genai_errors.APIError(504, {"error": {"code": 504, "message": str(exc) or "timeout", "status": "TIMEOUT"}})


def _thinking_config(model: str, budget: int) -> types.ThinkingConfig:
    """Gemini 3+ uses discrete thinking levels; 2.x uses a token budget."""
    if model.startswith("gemini-3"):
        level = "MINIMAL" if budget == 0 else "LOW" if budget <= 2048 else "MEDIUM"
        return types.ThinkingConfig(thinking_level=types.ThinkingLevel(level))
    return types.ThinkingConfig(thinking_budget=budget)


def _normalize_rows(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (matrix / norms).astype(np.float32)


class GeminiClient:
    """:class:`LLMClient` implementation backed by the Gemini API."""

    def __init__(self, settings: Settings) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise LLMError("GEMINI_API_KEY is not configured on the server.")
        self._settings = settings
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=settings.llm_timeout_seconds * 1000,
                # One quick retry for transient server errors; quota errors (429) go
                # straight to the next model instead of waiting.
                retry_options=types.HttpRetryOptions(
                    attempts=2, initial_delay=1.0, max_delay=4.0, http_status_codes=[500, 502, 503, 504]
                ),
            ),
        )
        self._router = ModelRouter(settings.fallback_models)
        self._embed_cache = _LRUCache(max_items=8000)
        self._embed_semaphore = asyncio.Semaphore(_EMBED_CONCURRENCY)

    # -- helpers ------------------------------------------------------------------------

    def _record_failure(self, model: str, exc: genai_errors.APIError, variant: str = "") -> None:
        if exc.code == 429:
            self._router.cool_down(model, _retry_after(exc), variant)
        logger.warning("Model %s%s failed with %s; trying the next model", model, variant, exc.code)

    async def _with_fallback(self, model: str, call: Callable[[str], Awaitable[ResultT]]) -> ResultT:
        """Run ``call`` on the first healthy model of the chain."""
        first_error: genai_errors.APIError | None = None
        for index, candidate in enumerate(self._router.candidates(model)):
            try:
                return await call(candidate)
            except (genai_errors.APIError, *_TRANSIENT_NETWORK_ERRORS) as raw:
                exc = _as_api_error(raw)
                first_error = first_error or exc
                if index == 0 and exc.code not in _RETRYABLE_CODES:
                    break  # a bad request would fail on every model
                self._record_failure(candidate, exc)
        assert first_error is not None
        raise _friendly_error(first_error) from first_error

    # -- embeddings ---------------------------------------------------------------------

    async def embed(self, texts: Sequence[str], task: EmbedTask) -> np.ndarray:
        """Return L2-normalised embeddings, one row per input text."""
        model = self._settings.gemini_embedding_model
        dim = self._settings.embedding_dimensions
        keys = [hashlib.sha256(f"{model}|{task}|{dim}|{t}".encode()).hexdigest() for t in texts]
        result: list[np.ndarray | None] = [self._embed_cache.get(k) for k in keys]
        missing = [i for i, vec in enumerate(result) if vec is None]

        async def embed_batch(indices: list[int]) -> None:
            async with self._embed_semaphore:
                batch: list[types.ContentUnion] = [texts[i] for i in indices]
                response = await self._client.aio.models.embed_content(
                    model=model,
                    contents=batch,
                    config=types.EmbedContentConfig(task_type=task, output_dimensionality=dim),
                )
            for i, embedding in zip(indices, response.embeddings or [], strict=True):
                vector = np.asarray(embedding.values, dtype=np.float32)
                result[i] = vector
                self._embed_cache.put(keys[i], vector)

        batches = [missing[i : i + _EMBED_BATCH_SIZE] for i in range(0, len(missing), _EMBED_BATCH_SIZE)]
        try:
            await asyncio.gather(*(embed_batch(batch) for batch in batches))
        except (genai_errors.APIError, *_TRANSIENT_NETWORK_ERRORS) as raw:
            exc = _as_api_error(raw)
            raise _friendly_error(exc) from raw
        if not texts:
            return np.zeros((0, dim), dtype=np.float32)
        return _normalize_rows(np.vstack([vec for vec in result if vec is not None]))

    # -- structured generation ------------------------------------------------------------

    async def generate_json(
        self, *, system: str, prompt: str, schema: type[ModelT], thinking_budget: int | None = None
    ) -> ModelT:
        budget = self._settings.analysis_thinking_budget if thinking_budget is None else thinking_budget

        async def call(model: str) -> ModelT:
            response = await self._client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0.2,
                    thinking_config=_thinking_config(model, budget),
                    automatic_function_calling=_NO_AFC,
                ),
            )
            if isinstance(response.parsed, schema):
                return response.parsed
            try:
                return schema.model_validate_json(response.text or "")
            except ValidationError as exc:
                raise LLMError("The AI returned an incomplete result. Please try again.") from exc

        return await self._with_fallback(self._settings.gemini_analysis_model, call)

    # -- streaming chat -------------------------------------------------------------------

    async def stream_chat(
        self,
        *,
        system: str,
        messages: Sequence[ChatMessage],
        web_search: bool,
        thinking_budget: int,
    ) -> AsyncIterator[StreamEvent]:
        contents: list[types.ContentUnion] = [
            types.Content(role=m.role, parts=[types.Part(text=m.text)]) for m in messages
        ]
        primary = self._settings.gemini_chat_model
        # Search grounding has its own quota, so it is tracked as a separate "+search" variant.
        # Last resort: answer without search rather than not at all.
        attempts = [(m, True) for m in self._router.candidates(primary, "+search")] if web_search else []
        attempts += [(m, False) for m in self._router.candidates(primary)]

        first_error: genai_errors.APIError | None = None
        for model, use_search in attempts:
            config = types.GenerateContentConfig(
                system_instruction=system,
                temperature=0.3,
                tools=[types.Tool(google_search=types.GoogleSearch())] if use_search else None,
                thinking_config=_thinking_config(model, thinking_budget),
                automatic_function_calling=_NO_AFC,
            )
            yielded_text = False
            try:
                stream = await self._client.aio.models.generate_content_stream(
                    model=model, contents=contents, config=config
                )
                async for chunk in stream:
                    event = _to_stream_event(chunk)
                    yielded_text = yielded_text or bool(event.text)
                    if event.text or event.web_sources or event.search_queries:
                        yield event
                return
            except (genai_errors.APIError, *_TRANSIENT_NETWORK_ERRORS) as raw:
                exc = _as_api_error(raw)
                if yielded_text:  # cannot restart an answer the user is already reading
                    raise _friendly_error(exc) from exc
                first_error = first_error or exc
                self._record_failure(model, exc, "+search" if use_search else "")
        assert first_error is not None
        raise _friendly_error(first_error) from first_error

    # -- media -------------------------------------------------------------------------------

    async def transcribe(self, data: bytes, mime_type: str) -> str:
        return await self.extract_text(data, mime_type, TRANSCRIBE_PROMPT)

    async def extract_text(self, data: bytes, mime_type: str, instruction: str) -> str:
        contents: list[types.PartUnion] = [types.Part.from_bytes(data=data, mime_type=mime_type), instruction]

        async def call(model: str) -> str:
            response = await self._client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    thinking_config=_thinking_config(model, 0),
                    automatic_function_calling=_NO_AFC,
                ),
            )
            return _response_text(response).strip()

        return await self._with_fallback(self._settings.gemini_transcribe_model, call)


def _response_text(response: types.GenerateContentResponse) -> str:
    parts: list[str] = []
    for candidate in response.candidates or []:
        if candidate.content:
            parts.extend(p.text for p in candidate.content.parts or [] if p.text and not p.thought)
    return "".join(parts)


def _to_stream_event(chunk: types.GenerateContentResponse) -> StreamEvent:
    event = StreamEvent(text=_response_text(chunk))
    for candidate in chunk.candidates or []:
        metadata = candidate.grounding_metadata
        if not metadata:
            continue
        event.search_queries.extend(metadata.web_search_queries or [])
        for grounding_chunk in metadata.grounding_chunks or []:
            web = grounding_chunk.web
            if web and web.uri:
                event.web_sources.append((web.title or web.domain or "Source", web.uri))
    return event


def _friendly_error(exc: genai_errors.APIError) -> LLMError:
    logger.error("Gemini API error %s: %s", exc.code, exc.message)
    if exc.code == 429:
        return LLMError("The AI service is busy (rate limit reached). Please wait a moment and retry.")
    if exc.code in (500, 502, 503, 504):
        return LLMError("The AI service is temporarily overloaded. Please try again shortly.")
    if exc.code in (401, 403):
        return LLMError("The AI service rejected the server's credentials.")
    if exc.code == 400:
        return LLMError("The AI service could not process this request (file may be unsupported or too large).")
    return LLMError("The AI service returned an unexpected error.")
