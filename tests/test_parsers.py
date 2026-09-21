from __future__ import annotations

import io

import pytest

from app.services import parsers
from app.services.parsers import DocumentError, detect_file_type, parse_document, safe_filename
from tests.conftest import FakeLLM
from tests.helpers import PNG_BYTES, make_docx, make_pdf, make_pptx, make_wav, make_xlsx, make_zip


@pytest.fixture
def media() -> FakeLLM:
    return FakeLLM()


class TestValidation:
    def test_rejects_unknown_extension(self) -> None:
        with pytest.raises(DocumentError, match="Unsupported file type"):
            detect_file_type("virus.exe", b"MZ....")

    def test_rejects_empty_file(self) -> None:
        with pytest.raises(DocumentError, match="empty"):
            detect_file_type("a.txt", b"")

    def test_rejects_content_that_does_not_match_extension(self) -> None:
        with pytest.raises(DocumentError, match="does not match"):
            detect_file_type("contract.pdf", b"just some text pretending to be a pdf")

    def test_rejects_binary_disguised_as_text(self) -> None:
        with pytest.raises(DocumentError, match="does not match"):
            detect_file_type("notes.txt", b"\x00\x01\x02binary")

    def test_rejects_zip_bomb(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(parsers, "MAX_ZIP_UNCOMPRESSED", 1000)
        bomb = make_zip({"word/document.xml": b"0" * 5000})
        with pytest.raises(DocumentError, match="unsafe size"):
            detect_file_type("bomb.docx", bomb)

    def test_rejects_zip_that_is_not_the_claimed_office_type(self) -> None:
        with pytest.raises(DocumentError, match="not a valid document"):
            detect_file_type("fake.docx", make_zip({"hello.txt": b"hi"}))

    def test_accepts_known_types(self) -> None:
        assert detect_file_type("a.PDF", make_pdf(["Hello"])).kind == "pdf"
        assert detect_file_type("voice.wav", make_wav()).kind == "audio"
        assert detect_file_type("scan.png", PNG_BYTES).kind == "image"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("../../etc/passwd", "passwd"),
            ("C:\\Users\\me\\lease.pdf", "lease.pdf"),
            ("bad\x00name\x1f.txt", "badname.txt"),
            ("", "document"),
        ],
    )
    def test_safe_filename(self, raw: str, expected: str) -> None:
        assert safe_filename(raw) == expected


class TestParsing:
    async def test_pdf_pages_become_located_segments(self, media: FakeLLM) -> None:
        pdf = make_pdf(["LEASE AGREEMENT\nThe rent is Rs. 25000 per month", "Signed at Chennai by both parties"])
        parsed = await parse_document("lease.pdf", pdf, media)
        assert [s.location for s in parsed.segments] == ["Page 1", "Page 2"]
        assert "Rs. 25000" in parsed.segments[0].text
        assert media.calls == []  # text layer present: no OCR needed

    async def test_scanned_pdf_falls_back_to_ocr(self, media: FakeLLM) -> None:
        parsed = await parse_document("scan.pdf", make_pdf(["", ""]), media)
        assert ("ocr", "application/pdf") in media.calls
        assert [s.location for s in parsed.segments] == ["Page 1", "Page 2"]
        assert any("OCR" in note for note in parsed.notes)

    async def test_password_protected_pdf_is_rejected(self, media: FakeLLM) -> None:
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=200, height=200)
        writer.encrypt("secret", algorithm="RC4-128")
        buffer = io.BytesIO()
        writer.write(buffer)
        with pytest.raises(DocumentError, match="password"):
            await parse_document("locked.pdf", buffer.getvalue(), media)

    async def test_docx_paragraphs_and_tables(self, media: FakeLLM) -> None:
        parsed = await parse_document("contract.docx", make_docx(), media)
        assert "deliver the software by 30 June 2026" in parsed.text
        assert "Delivery | Rs. 1,00,000" in parsed.text

    async def test_xlsx_rows_with_sheet_location(self, media: FakeLLM) -> None:
        parsed = await parse_document("ledger.xlsx", make_xlsx(), media)
        assert parsed.segments[0].location.startswith("Sheet 'Payments' rows 1")
        assert "May | 25000 | Late" in parsed.text

    async def test_pptx_slides(self, media: FakeLLM) -> None:
        parsed = await parse_document("deck.pptx", make_pptx(), media)
        assert parsed.segments[0].location == "Slide 1"
        assert "Refunds are processed within 14 days." in parsed.text

    async def test_csv_and_tsv(self, media: FakeLLM) -> None:
        csv_doc = await parse_document("a.csv", b"name,amount\nRent,25000\n", media)
        tsv_doc = await parse_document("a.tsv", b"name\tamount\nRent\t25000\n", media)
        assert "Rent | 25000" in csv_doc.text
        assert "Rent | 25000" in tsv_doc.text

    async def test_html_drops_scripts(self, media: FakeLLM) -> None:
        html = b"<html><head><script>alert(1)</script></head><body><h1>Policy</h1><p>No refunds.</p></body></html>"
        parsed = await parse_document("policy.html", html, media)
        assert "No refunds." in parsed.text
        assert "alert" not in parsed.text

    async def test_email_headers_body_and_attachments(self, media: FakeLLM) -> None:
        eml = (
            b"From: landlord@example.com\r\nTo: tenant@example.com\r\nSubject: Notice to vacate\r\n"
            b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary="b"\r\n\r\n'
            b"--b\r\nContent-Type: text/plain\r\n\r\nPlease vacate by 30 June.\r\n"
            b'--b\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename="notice.pdf"\r\n\r\n'
            b"%PDF-1.4\r\n--b--\r\n"
        )
        parsed = await parse_document("notice.eml", eml, media)
        assert parsed.segments[0].location == "Email headers"
        assert "Subject: Notice to vacate" in parsed.text
        assert "Please vacate by 30 June." in parsed.text
        assert "notice.pdf" in parsed.text

    async def test_media_larger_than_the_inline_limit_is_rejected(
        self, media: FakeLLM, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(parsers, "MAX_INLINE_MEDIA_BYTES", 10)
        with pytest.raises(DocumentError, match="limited to"):
            await parse_document("photo.png", PNG_BYTES, media)
        assert media.calls == []  # rejected before any Gemini call

    def test_page_limit_is_1500(self) -> None:
        assert parsers.MAX_PDF_PAGES == 1500

    async def test_image_uses_ocr(self, media: FakeLLM) -> None:
        parsed = await parse_document("photo.png", PNG_BYTES, media)
        assert ("ocr", "image/png") in media.calls
        assert "Rs. 9,000" in parsed.text

    async def test_audio_is_transcribed(self, media: FakeLLM) -> None:
        parsed = await parse_document("note.wav", make_wav(), media)
        assert parsed.segments[0].location == "Recording"
        assert parsed.text == "My landlord kept my deposit."

    async def test_utf16_and_cp1252_text(self, media: FakeLLM) -> None:
        utf16 = await parse_document("a.txt", "Café lease".encode("utf-16"), media)
        cp1252 = await parse_document("b.txt", "Caf\xe9 lease".encode("cp1252"), media)
        assert utf16.text == "Café lease"
        assert cp1252.text == "Café lease"

    async def test_corrupted_office_file_is_a_document_error(self, media: FakeLLM) -> None:
        broken = make_zip({"word/document.xml": b"<not-xml"})
        with pytest.raises(DocumentError):
            await parse_document("broken.docx", broken, media)

    async def test_file_without_text_is_rejected(self, media: FakeLLM) -> None:
        with pytest.raises(DocumentError, match="No readable text"):
            await parse_document("blank.txt", b"   \n\n  ", media)
