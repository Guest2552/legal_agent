"""The document domain model shared by ingestion, retrieval, analysis and storage."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from app.schemas import DocumentInfo
from app.services.chunking import Chunk
from app.services.parsers import Segment


@dataclass(slots=True)
class StoredDocument:
    """A parsed document: located text segments, retrieval chunks and optional embeddings."""

    id: str
    name: str
    kind: str
    size_bytes: int
    sha256: str
    segments: list[Segment]
    chunks: list[Chunk]
    vectors: np.ndarray | None  # one row per chunk, or None when embeddings were unavailable
    notes: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @property
    def characters(self) -> int:
        return sum(len(s.text) for s in self.segments)

    @property
    def full_text(self) -> str:
        return "\n\n".join(s.text for s in self.segments)

    def marked_text(self, limit: int) -> tuple[str, bool]:
        """Full text with ``[location]`` markers, truncated to ``limit`` characters."""
        parts: list[str] = []
        used = 0
        for segment in self.segments:
            block = f"[{segment.location}]\n{segment.text}"
            if used + len(block) > limit:
                remaining = limit - used
                if remaining > 200:
                    parts.append(block[:remaining])
                return "\n\n".join(parts), True
            parts.append(block)
            used += len(block) + 2
        return "\n\n".join(parts), False

    def info(self) -> DocumentInfo:
        return DocumentInfo(
            id=self.id,
            name=self.name,
            kind=self.kind,
            size_bytes=self.size_bytes,
            characters=self.characters,
            chunks=len(self.chunks),
            locations=len(self.segments),
            semantic_search=self.vectors is not None,
            notes=self.notes,
            created_at=self.created_at,
        )
