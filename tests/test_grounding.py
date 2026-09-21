"""Deterministic hallucination checks: citations and verbatim quotes."""

from __future__ import annotations

from app.services.grounding import QuoteVerifier, annotate_quotes, extract_citations, grounding_report

SOURCE = (
    "4. SECURITY DEPOSIT\nThe Tenant has paid an interest-free security deposit of Rs. 50,000 "
    "(Rupees Fifty Thousand only). The deposit shall be refunded within 30 days after the Tenant vacates."
)
SOURCES = {"S1": SOURCE, "S2": "Either party may terminate this Agreement by giving one month's written notice."}


class TestQuoteVerifier:
    def test_exact_quote_is_verified_ignoring_case_whitespace_and_typography(self) -> None:
        check = QuoteVerifier(SOURCES).check("the deposit shall be   refunded within 30 days after the tenant’s")
        assert check.status in {"verified", "partial"}
        exact = QuoteVerifier(SOURCES).check("THE DEPOSIT SHALL BE REFUNDED\nwithin 30 days")
        assert exact.status == "verified"
        assert exact.source_id == "S1"
        assert exact.coverage == 1.0

    def test_fabricated_quote_is_unverified(self) -> None:
        check = QuoteVerifier(SOURCES).check("the landlord may keep the full deposit for repainting")
        assert check.status == "unverified"
        assert check.source_id is None

    def test_one_changed_word_in_long_quote_is_only_partial(self) -> None:
        tampered = "The Tenant has paid an interest-free security deposit of Rs. 90,000 (Rupees Fifty Thousand only)"
        assert QuoteVerifier(SOURCES).check(tampered).status == "partial"

    def test_changed_number_in_short_quote_fails(self) -> None:
        assert QuoteVerifier(SOURCES).check("refunded within 60 days").status == "unverified"

    def test_restrict_to_specific_sources(self) -> None:
        verifier = QuoteVerifier(SOURCES)
        assert verifier.check("giving one month's written notice", only={"S1"}).status == "unverified"
        assert verifier.check("giving one month's written notice", only={"S2"}).status == "verified"

    def test_elided_quote_is_checked_fragment_by_fragment(self) -> None:
        verifier = QuoteVerifier(SOURCES)
        genuine = "The Tenant has paid an interest-free security deposit ... shall be refunded within 30 days"
        assert verifier.check(genuine).status == "verified"
        spliced = "The Tenant has paid an interest-free security deposit ... which the landlord may keep forever"
        assert verifier.check(spliced).status == "unverified"

    def test_empty_quote(self) -> None:
        assert QuoteVerifier(SOURCES).check("  ").status == "none"


class TestCitations:
    def test_extracts_grouped_and_adjacent_citations_in_order(self) -> None:
        assert extract_citations("A [S2]. B [S1, S3]; C [S2][S4] and [S5;S6]") == ["S2", "S1", "S3", "S4", "S5", "S6"]

    def test_grounded_answer(self) -> None:
        answer = 'The lease says "The deposit shall be refunded within 30 days after the Tenant vacates" [S1].'
        report = grounding_report(answer, SOURCES)
        assert report.verdict == "grounded"
        assert report.cited_sources == ["S1"]
        assert report.quotes[0].status == "verified"

    def test_short_quoted_title_does_not_break_quote_pairing(self) -> None:
        answer = (
            'Clause "EARLY EXIT," says "The deposit shall be refunded within 30 days after the Tenant vacates" [S1].'
        )
        report = grounding_report(answer, SOURCES)
        assert [q.status for q in report.quotes] == ["verified"]

    def test_invented_citation_is_flagged(self) -> None:
        report = grounding_report("The penalty is 10% [S9].", SOURCES)
        assert report.verdict == "unverified"
        assert report.invalid_citations == ["S9"]

    def test_misquoted_clause_is_flagged(self) -> None:
        answer = 'It says "the landlord may deduct any amount he likes from the deposit" [S1].'
        report = grounding_report(answer, SOURCES)
        assert report.verdict == "unverified"
        assert report.quotes[0].status == "unverified"

    def test_quotes_of_law_without_document_citation_are_not_penalised(self) -> None:
        answer = 'General law: courts award "reasonable compensation not exceeding the amount so named".'
        report = grounding_report(answer, SOURCES)
        assert report.verdict == "general_knowledge"
        assert report.quotes == []


def test_annotate_quotes_marks_every_quote_field() -> None:
    data = {
        "clauses": [
            {"title": "Deposit", "quote": "security deposit of Rs. 50,000"},
            {"title": "Invented", "quote": "tenant forfeits everything"},
            {"title": "No quote", "quote": ""},
        ],
        "inconsistencies": [{"quote": "one month's written notice", "conflicting_quote": "three months notice"}],
    }
    verifier = QuoteVerifier(SOURCES)
    counts = annotate_quotes(data, {"quote": verifier, "conflicting_quote": verifier})
    statuses = [c["quote_status"] for c in data["clauses"]]
    assert statuses == ["verified", "unverified", "none"]
    assert data["inconsistencies"][0]["conflicting_quote_status"] == "unverified"
    assert counts == {"verified": 2, "partial": 0, "unverified": 2}
