"""Shared fixtures. A deterministic FakeLLM replaces Gemini so tests are fast and offline."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.config import Settings
from app.main import create_app
from app.services.llm import ChatMessage, StreamEvent
from app.services.retrieval import tokenize

LEASE_TEXT = """RESIDENTIAL LEASE AGREEMENT
1. RENT
The monthly rent is Rs. 25,000 payable on or before the 5th day of every month.
2. SECURITY DEPOSIT
The tenant has paid a security deposit of Rs. 50,000 which shall be refunded within 30 days of vacating.
3. TERMINATION
Either party may terminate this lease by giving one month's written notice.
"""

GROUNDED_ANSWER = (
    "Your deposit must be returned within 30 days. The lease says "
    '"a security deposit of Rs. 50,000 which shall be refunded within 30 days of vacating" [S1].\n\n'
    "**General law:** unfair deductions can be challenged."
)

EMBED_DIM = 64


def fake_vector(text: str) -> np.ndarray:
    """Deterministic bag-of-words embedding: similar words -> similar vectors."""
    vector = np.zeros(EMBED_DIM, dtype=np.float32)
    for token in tokenize(text):
        vector[int(hashlib.md5(token.encode()).hexdigest(), 16) % EMBED_DIM] += 1.0
    norm = np.linalg.norm(vector)
    return vector / norm if norm else vector


class FakeLLM:
    """Implements the LLMClient protocol without any network access."""

    def __init__(self) -> None:
        self.answer = GROUNDED_ANSWER
        self.json_results: dict[type[BaseModel], BaseModel] = {}
        self.calls: list[tuple[str, Any]] = []
        self.fail_embeddings = False
        self.web_sources = [("Ministry of Housing", "https://example.gov.in/tenancy")]

    async def embed(self, texts: Sequence[str], task: str) -> np.ndarray:
        self.calls.append(("embed", len(texts)))
        if self.fail_embeddings:
            from app.services.llm import LLMError

            raise LLMError("embedding quota exhausted")
        return np.vstack([fake_vector(t) for t in texts]) if texts else np.zeros((0, EMBED_DIM), np.float32)

    async def generate_json(
        self, *, system: str, prompt: str, schema: type[BaseModel], thinking_budget: int | None = None
    ) -> BaseModel:
        self.calls.append(("json", schema.__name__))
        return self.json_results[schema].model_copy(deep=True)

    async def stream_chat(
        self, *, system: str, messages: Sequence[ChatMessage], web_search: bool, thinking_budget: int
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append(("chat", {"system": system, "messages": list(messages), "web_search": web_search}))
        for start in range(0, len(self.answer), 25):
            yield StreamEvent(text=self.answer[start : start + 25])
        if web_search:
            yield StreamEvent(web_sources=self.web_sources, search_queries=["tenant deposit refund law"])

    async def transcribe(self, data: bytes, mime_type: str) -> str:
        self.calls.append(("transcribe", mime_type))
        return "My landlord kept my deposit."

    async def extract_text(self, data: bytes, mime_type: str, instruction: str) -> str:
        self.calls.append(("ocr", mime_type))
        return "[Page 1]\nScanned lease: the rent is Rs. 9,000.\n[Page 2]\nSigned by both parties."


@pytest.fixture(autouse=True)
def isolated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own SQLite file (Settings reads DATABASE_PATH from the env)."""
    path = tmp_path / "lexiguide-test.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    return path


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,  # never read the developer's real .env in tests
        gemini_api_key="test-key",
        enable_api_docs=False,
        ai_requests_per_minute=1000,
        log_level="WARNING",
    )


@pytest.fixture
def client(settings: Settings, fake_llm: FakeLLM) -> TestClient:
    return TestClient(create_app(settings, llm=fake_llm))


def upload_text(client: TestClient, name: str = "lease.txt", text: str = LEASE_TEXT) -> dict[str, Any]:
    response = client.post("/api/documents", files={"file": (name, text.encode(), "text/plain")})
    assert response.status_code == 201, response.text
    return response.json()


def parse_sse(body: str) -> list[tuple[str, Any]]:
    import json

    events = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        events.append((lines["event"], json.loads(lines["data"])))
    return events
