"""Structure-aware chunking for retrieval.

Chunks never cross a segment boundary (so every chunk has one precise location such as
``Page 4``), prefer line/sentence boundaries, carry a small overlap, and remember the
nearest clause heading ("12. Termination") so answers can cite it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.services.parsers import Segment

_KEYWORD_HEADING = re.compile(
    r"^(article|section|clause|schedule|annex(ure)?|appendix|part|chapter|exhibit)\b", re.IGNORECASE
)
_NUMBERED_HEADING = re.compile(r"^(\d{1,3}(\.\d{1,3}){0,3}\.?|[IVXLC]{1,6}\.|\([a-z]\))\s+\S")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?;])\s+")
_MAX_HEADING_CHARS = 90
_MAX_HEADING_WORDS = 12


@dataclass(frozen=True, slots=True)
class Chunk:
    id: str
    doc_id: str
    doc_name: str
    index: int
    text: str
    location: str
    section: str

    @property
    def embedding_text(self) -> str:
        """Text sent to the embedding model: prefixed with its context for better recall."""
        context = self.doc_name + (f" / {self.section}" if self.section else "")
        return f"{context}\n{self.text}"


def detect_heading(line: str) -> str | None:
    """Return ``line`` if it looks like a clause/section heading, else ``None``."""
    line = " ".join(line.split())
    words = line.split()
    if not line or len(line) > _MAX_HEADING_CHARS or len(words) > _MAX_HEADING_WORDS:
        return None
    if line.endswith((",", ";")) or (line.endswith(".") and len(words) > 6):
        return None
    letters = [ch for ch in line if ch.isalpha()]
    is_all_caps = len(letters) >= 3 and all(ch.isupper() for ch in letters)
    if _KEYWORD_HEADING.match(line) or _NUMBERED_HEADING.match(line) or is_all_caps:
        return line
    return None


def _hard_wrap(text: str, size: int) -> list[str]:
    pieces = []
    while len(text) > size:
        cut = text.rfind(" ", 0, size)
        cut = cut if cut > size // 2 else size
        pieces.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        pieces.append(text)
    return pieces


def split_long_line(line: str, size: int) -> list[str]:
    """Split an over-long line on sentence boundaries, hard-wrapping as a last resort."""
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_BREAK.split(line):
        for part in _hard_wrap(sentence, size) if len(sentence) > size else [sentence]:
            if current and len(current) + len(part) + 1 > size:
                pieces.append(current)
                current = part
            else:
                current = f"{current} {part}".strip()
    if current:
        pieces.append(current)
    return pieces


def _overlap_tail(lines: list[str], overlap: int) -> list[str]:
    tail: list[str] = []
    total = 0
    for line in reversed(lines):
        if total + len(line) > overlap:
            break
        tail.insert(0, line)
        total += len(line) + 1
    return tail


def chunk_segments(
    doc_id: str,
    doc_name: str,
    segments: Sequence[Segment],
    size: int = 1200,
    overlap: int = 200,
) -> list[Chunk]:
    """Split located segments into overlapping retrieval chunks."""
    chunks: list[Chunk] = []
    section = ""

    def emit(lines: list[str], chunk_section: str, location: str) -> None:
        chunks.append(
            Chunk(
                id=f"{doc_id}:{len(chunks)}",
                doc_id=doc_id,
                doc_name=doc_name,
                index=len(chunks),
                text="\n".join(lines),
                location=location,
                section=chunk_section,
            )
        )

    for segment in segments:
        buffer: list[str] = []
        length = 0
        fresh = False  # does the buffer hold anything beyond carried-over overlap?
        chunk_section = section
        for raw_line in segment.text.split("\n"):
            line = raw_line.strip()
            if not line:
                continue
            heading = detect_heading(line)
            for piece in [line] if len(line) <= size else split_long_line(line, size):
                if fresh and length + len(piece) + 1 > size:
                    emit(buffer, chunk_section, segment.location)
                    buffer = _overlap_tail(buffer, overlap)
                    length = sum(len(x) + 1 for x in buffer)
                    fresh = False
                if heading:
                    section = heading
                    heading = None
                if not fresh:
                    chunk_section = section
                buffer.append(piece)
                length += len(piece) + 1
                fresh = True
        if fresh:
            emit(buffer, chunk_section, segment.location)
    return chunks
