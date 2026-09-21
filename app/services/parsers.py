"""File ingestion: validate uploads and turn them into located text segments.

Supported: PDF (incl. scanned, via Gemini OCR), Word (.docx), Excel (.xlsx/.xlsm),
CSV/TSV, PowerPoint (.pptx), plain text/Markdown/JSON, HTML, e-mail (.eml),
images (OCR) and audio / voice notes (transcription).

Security: the extension must be on an allow-list *and* the bytes must match the
expected file signature; ZIP-based Office files are checked for zip bombs; parsing
is bounded (pages, rows, slides) so a hostile file cannot exhaust memory or CPU.
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
import unicodedata
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import PurePath
from typing import Protocol

from app.prompts import OCR_IMAGE_PROMPT, OCR_PDF_PROMPT

MAX_PDF_PAGES = 1500
MAX_INLINE_MEDIA_BYTES = 19 * 1024 * 1024  # Gemini inline-request limit (OCR / audio)
MAX_SHEET_ROWS = 5000
MAX_SHEET_COLS = 200
ROWS_PER_SEGMENT = 50
LINES_PER_SEGMENT = 60
PARAGRAPHS_PER_SEGMENT = 40
MAX_ZIP_ENTRIES = 3000
MAX_ZIP_UNCOMPRESSED = 250 * 1024 * 1024
MIN_CHARS_PER_PDF_PAGE = 25  # below this we treat the PDF as scanned and OCR it


class DocumentError(ValueError):
    """The file is unsupported, malformed or empty. Message is user-safe."""


class MediaReader(Protocol):
    """The subset of the LLM client needed to read images, scans and audio."""

    async def transcribe(self, data: bytes, mime_type: str) -> str: ...

    async def extract_text(self, data: bytes, mime_type: str, instruction: str) -> str: ...


@dataclass(frozen=True, slots=True)
class FileType:
    kind: str
    mime: str


@dataclass(frozen=True, slots=True)
class Segment:
    """A span of text plus a human-readable location such as ``Page 3``."""

    text: str
    location: str


@dataclass(slots=True)
class ParsedDocument:
    kind: str
    segments: list[Segment]
    notes: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(s.text for s in self.segments)


_OOXML = "application/vnd.openxmlformats-officedocument"
FILE_TYPES: dict[str, FileType] = {
    ".pdf": FileType("pdf", "application/pdf"),
    ".docx": FileType("docx", f"{_OOXML}.wordprocessingml.document"),
    ".xlsx": FileType("xlsx", f"{_OOXML}.spreadsheetml.sheet"),
    ".xlsm": FileType("xlsx", "application/vnd.ms-excel.sheet.macroEnabled.12"),
    ".pptx": FileType("pptx", f"{_OOXML}.presentationml.presentation"),
    ".csv": FileType("csv", "text/csv"),
    ".tsv": FileType("csv", "text/tab-separated-values"),
    ".txt": FileType("text", "text/plain"),
    ".md": FileType("text", "text/markdown"),
    ".json": FileType("text", "application/json"),
    ".html": FileType("html", "text/html"),
    ".htm": FileType("html", "text/html"),
    ".eml": FileType("email", "message/rfc822"),
    ".png": FileType("image", "image/png"),
    ".jpg": FileType("image", "image/jpeg"),
    ".jpeg": FileType("image", "image/jpeg"),
    ".webp": FileType("image", "image/webp"),
    ".mp3": FileType("audio", "audio/mp3"),
    ".wav": FileType("audio", "audio/wav"),
    ".m4a": FileType("audio", "audio/mp4"),
    ".aac": FileType("audio", "audio/aac"),
    ".ogg": FileType("audio", "audio/ogg"),
    ".opus": FileType("audio", "audio/ogg"),
    ".flac": FileType("audio", "audio/flac"),
    ".webm": FileType("audio", "audio/webm"),
}
ACCEPTED_EXTENSIONS: tuple[str, ...] = tuple(FILE_TYPES)

_ZIP_REQUIRED_ENTRY = {
    "docx": "word/document.xml",
    "xlsx": "xl/workbook.xml",
    "pptx": "ppt/presentation.xml",
}

# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


def safe_filename(name: str | None, default: str = "document") -> str:
    """Strip directories and control characters; bound the length."""
    base = PurePath((name or "").replace("\\", "/")).name
    base = "".join(ch for ch in base if unicodedata.category(ch)[0] != "C").strip()
    return (base or default)[:120]


def _matches_signature(kind: str, data: bytes) -> bool:
    head = data[:16]
    if kind == "pdf":
        return b"%PDF-" in data[:1024]
    if kind in _ZIP_REQUIRED_ENTRY:
        return head.startswith(b"PK\x03\x04")
    if kind == "image":
        return (
            head.startswith(b"\x89PNG\r\n\x1a\n")
            or head.startswith(b"\xff\xd8\xff")
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
        )
    if kind == "audio":
        return (
            (head[:4] == b"RIFF" and head[8:12] == b"WAVE")
            or head.startswith((b"ID3", b"OggS", b"fLaC", b"\x1a\x45\xdf\xa3"))
            or head[4:8] == b"ftyp"
            or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
        )
    # Text-like formats: reject binary content.
    return b"\x00" not in data[:8192] or data.startswith((b"\xff\xfe", b"\xfe\xff"))


def detect_file_type(filename: str, data: bytes) -> FileType:
    """Return the file type or raise :class:`DocumentError` if it is not acceptable."""
    if not data:
        raise DocumentError("The file is empty.")
    extension = PurePath(filename.lower()).suffix
    file_type = FILE_TYPES.get(extension)
    if file_type is None:
        allowed = ", ".join(sorted({e.lstrip(".") for e in ACCEPTED_EXTENSIONS}))
        raise DocumentError(f"Unsupported file type '{extension or 'none'}'. Supported: {allowed}.")
    if not _matches_signature(file_type.kind, data):
        raise DocumentError("The file content does not match its extension.")
    if file_type.kind in _ZIP_REQUIRED_ENTRY:
        _check_zip(data, _ZIP_REQUIRED_ENTRY[file_type.kind])
    return file_type


def _check_zip(data: bytes, required_entry: str) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
    except zipfile.BadZipFile as exc:
        raise DocumentError("The Office file is corrupted.") from exc
    if len(entries) > MAX_ZIP_ENTRIES or sum(e.file_size for e in entries) > MAX_ZIP_UNCOMPRESSED:
        raise DocumentError("The Office file expands to an unsafe size.")
    if required_entry not in names:
        raise DocumentError("The Office file is not a valid document of this type.")


# --------------------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------------------

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MANY_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r"[ \t]+\n")


def clean_text(text: str) -> str:
    """Normalise newlines, drop control characters and excess blank lines."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS.sub("", text)
    text = _TRAILING_SPACE.sub("\n", text)
    return _MANY_BLANK_LINES.sub("\n\n", text).strip()


def decode_text(data: bytes) -> str:
    """Decode bytes that are expected to be text, tolerating common encodings."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _group(items: list[str], size: int, label: Callable[[int, int], str]) -> list[Segment]:
    """Group consecutive text items into segments labelled by their 1-based range."""
    segments = []
    for start in range(0, len(items), size):
        batch = items[start : start + size]
        text = clean_text("\n".join(batch))
        if text:
            segments.append(Segment(text, label(start + 1, start + len(batch))))
    return segments


def _segments_from_lines(text: str, unit: str = "Lines") -> list[Segment]:
    lines = clean_text(text).split("\n")
    return _group(lines, LINES_PER_SEGMENT, lambda a, b: f"{unit} {a}-{b}")


_PAGE_MARKER = re.compile(r"^\s*\[Page\s+(\d+)\]\s*$", re.MULTILINE)


def _segments_from_page_markers(text: str) -> list[Segment]:
    """Split OCR output that contains ``[Page N]`` marker lines."""
    parts = _PAGE_MARKER.split(text)
    if len(parts) < 3:
        return [Segment(clean_text(text), "Page 1")] if text.strip() else []
    segments = []
    for number, body in zip(parts[1::2], parts[2::2], strict=False):
        if body.strip():
            segments.append(Segment(clean_text(body), f"Page {number}"))
    return segments


# --------------------------------------------------------------------------------------
# Format-specific parsers (synchronous; run in a worker thread)
# --------------------------------------------------------------------------------------


def parse_pdf_text(data: bytes) -> list[Segment]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        encrypted = reader.is_encrypted and not reader.decrypt("")
    except PdfReadError as exc:
        raise DocumentError("The PDF could not be read; it may be corrupted.") from exc
    if encrypted:
        raise DocumentError("This PDF is password-protected. Please upload an unlocked copy.")
    if len(reader.pages) > MAX_PDF_PAGES:
        raise DocumentError(f"PDFs are limited to {MAX_PDF_PAGES} pages.")
    return [
        Segment(clean_text(page.extract_text() or ""), f"Page {number}")
        for number, page in enumerate(reader.pages, start=1)
    ]


def parse_docx(data: bytes) -> list[Segment]:
    from docx import Document
    from docx.table import Table

    document = Document(io.BytesIO(data))
    blocks: list[str] = []
    for item in document.iter_inner_content():
        if isinstance(item, Table):
            for row in item.rows:
                cells: list[str] = []
                for cell in row.cells:
                    value = cell.text.strip()
                    if value and (not cells or cells[-1] != value):  # skip merged duplicates
                        cells.append(value)
                if cells:
                    blocks.append(" | ".join(cells))
        elif item.text.strip():
            blocks.append(item.text.strip())
    return _group(blocks, PARAGRAPHS_PER_SEGMENT, lambda a, b: f"Paragraphs {a}-{b}")


def _rows_to_segments(rows: Iterable[Iterable[object]], sheet: str | None) -> list[Segment]:
    lines: list[str] = []
    header = ""
    for row_number, row in enumerate(rows, start=1):
        if row_number > MAX_SHEET_ROWS:
            break
        values = [str(v).strip() for v in list(row)[:MAX_SHEET_COLS] if v not in (None, "")]
        line = " | ".join(v for v in values if v)
        if line and not header:
            header = line
        lines.append(line)
    prefix = f"Sheet '{sheet}' " if sheet else ""
    segments = []
    for start in range(0, len(lines), ROWS_PER_SEGMENT):
        batch = [line for line in lines[start : start + ROWS_PER_SEGMENT] if line]
        if not batch:
            continue
        if start > 0 and header:
            batch.insert(0, f"(Columns: {header})")
        end = min(start + ROWS_PER_SEGMENT, len(lines))
        segments.append(Segment("\n".join(batch), f"{prefix}rows {start + 1}-{end}".strip()))
    return segments


def parse_xlsx(data: bytes) -> list[Segment]:
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        segments: list[Segment] = []
        for sheet in workbook.worksheets:
            segments.extend(_rows_to_segments(sheet.iter_rows(values_only=True), sheet.title))
        return segments
    finally:
        workbook.close()


def parse_csv(data: bytes, delimiter: str) -> list[Segment]:
    reader = csv.reader(io.StringIO(decode_text(data)), delimiter=delimiter)
    return _rows_to_segments(reader, None)


def parse_pptx(data: bytes) -> list[Segment]:
    from pptx import Presentation

    presentation = Presentation(io.BytesIO(data))
    segments = []
    for number, slide in enumerate(presentation.slides, start=1):
        texts: list[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                texts.append(shape.text_frame.text.strip())
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    texts.append(" | ".join(c.text.strip() for c in row.cells if c.text.strip()))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                texts.append(f"Speaker notes: {notes}")
        text = clean_text("\n".join(t for t in texts if t))
        if text:
            segments.append(Segment(text, f"Slide {number}"))
    return segments


class _HTMLTextExtractor(HTMLParser):
    _SKIP = frozenset({"script", "style", "noscript", "template", "svg", "head"})
    _BLOCK = frozenset(
        {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BLOCK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        return re.sub(r"[ \t]+", " ", "".join(self._parts))


def html_to_text(html: str) -> str:
    extractor = _HTMLTextExtractor()
    extractor.feed(html)
    extractor.close()
    return clean_text(extractor.text())


def parse_email(data: bytes) -> list[Segment]:
    message = BytesParser(policy=policy.default).parsebytes(data)
    header_lines = [f"{name}: {message[name]}" for name in ("From", "To", "Cc", "Date", "Subject") if message[name]]
    body = message.get_body(preferencelist=("plain", "html"))
    body_text = ""
    if body is not None:
        content = body.get_content()
        body_text = html_to_text(content) if body.get_content_type() == "text/html" else str(content)
    attachments = [name for part in message.iter_attachments() if (name := part.get_filename())]
    if attachments:
        body_text += "\n\nAttachments (not included): " + ", ".join(attachments)
    segments = [Segment("\n".join(header_lines), "Email headers")] if header_lines else []
    return segments + _segments_from_lines(body_text, unit="Email lines")


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def _parse_tsv(data: bytes) -> list[Segment]:
    return parse_csv(data, "\t")


def _parse_csv_default(data: bytes) -> list[Segment]:
    return parse_csv(data, ",")


def _parse_html(data: bytes) -> list[Segment]:
    return _segments_from_lines(html_to_text(decode_text(data)))


def _parse_plain(data: bytes) -> list[Segment]:
    return _segments_from_lines(decode_text(data))


# Local (CPU-bound) parsers, keyed by file kind. They run in a worker thread.
_LOCAL_PARSERS: dict[str, Callable[[bytes], list[Segment]]] = {
    "pdf": parse_pdf_text,
    "docx": parse_docx,
    "xlsx": parse_xlsx,
    "csv": _parse_csv_default,
    "pptx": parse_pptx,
    "html": _parse_html,
    "email": parse_email,
    "text": _parse_plain,
}


async def _parse_locally(parser: Callable[[bytes], list[Segment]], data: bytes) -> list[Segment]:
    try:
        return await asyncio.to_thread(parser, data)
    except DocumentError:
        raise
    except Exception as exc:  # untrusted input: any library error means "unreadable file"
        raise DocumentError("The file could not be read; it may be corrupted.") from exc


def _require_inline_size(data: bytes, what: str) -> None:
    """Media read by Gemini is sent inline, which caps the request size."""
    if len(data) > MAX_INLINE_MEDIA_BYTES:
        limit_mb = MAX_INLINE_MEDIA_BYTES // (1024 * 1024)
        raise DocumentError(f"{what} are limited to {limit_mb} MB. Please upload a smaller or text-based file.")


async def parse_document(filename: str, data: bytes, media: MediaReader) -> ParsedDocument:
    """Validate and parse ``data``. Raises :class:`DocumentError` on bad input."""
    file_type = detect_file_type(filename, data)
    kind = file_type.kind
    notes: list[str] = []
    segments: list[Segment]

    if kind == "image":
        _require_inline_size(data, "Images")
        segments = _segments_from_lines(await media.extract_text(data, file_type.mime, OCR_IMAGE_PROMPT))
        notes.append("Image: text was read with Gemini OCR.")
    elif kind == "audio":
        _require_inline_size(data, "Audio files")
        transcript = await media.transcribe(data, file_type.mime)
        segments = [Segment(clean_text(transcript), "Recording")] if transcript.strip() else []
        notes.append("Audio: transcribed with Gemini.")
    else:
        parser = _parse_tsv if filename.lower().endswith(".tsv") else _LOCAL_PARSERS[kind]
        segments = await _parse_locally(parser, data)
        if kind == "pdf" and sum(len(s.text) for s in segments) < MIN_CHARS_PER_PDF_PAGE * max(len(segments), 1):
            _require_inline_size(data, "Scanned PDFs (without a text layer)")
            segments = _segments_from_page_markers(await media.extract_text(data, file_type.mime, OCR_PDF_PROMPT))
            notes.append("Scanned PDF: text was read with Gemini OCR.")

    segments = [s for s in segments if s.text.strip()]
    if not segments:
        raise DocumentError("No readable text was found in this file.")
    return ParsedDocument(kind=kind, segments=segments, notes=notes)
