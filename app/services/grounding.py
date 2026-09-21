"""Deterministic anti-hallucination checks.

The model is required to back document claims with verbatim quotes and ``[S#]``
citations. This module verifies them *without* another model call:

* every citation must reference a source that was actually provided;
* every quote is matched against the source text (normalised exact match, then word
  trigram coverage), yielding ``verified`` / ``partial`` / ``unverified``.

The results are shown to the user next to each answer and analysis item.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from app.schemas import GroundingReport, GroundingVerdict, QuoteCheck, QuoteStatus
from app.services.textutils import words

_PUNCT_MAP = str.maketrans(
    {
        "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2018": "'", "\u2019": "'",
        "\u2013": "-", "\u2014": "-", "\u2011": "-", "\u00a0": " ",
    }
)  # fmt: skip
_CITATION = re.compile(r"\[\s*(S\d+(?:\s*[,;]\s*S\d+)*)\s*\]")
# Quotes are matched in pairs (straight or curly), so a short quoted phrase such as
# "EARLY EXIT," cannot swallow the opening mark of the real quote that follows it.
_OPEN, _CLOSE = chr(0x201C), chr(0x201D)
_QUOTED = re.compile(rf'"([^"\n]{{1,600}})"|{_OPEN}([^{_CLOSE}\n]{{1,600}}){_CLOSE}')
_CITATION_WINDOW = 80  # characters after a quote in which a citation ties it to a source
_NGRAM = 3
VERIFIED_AT = 0.9
PARTIAL_AT = 0.6
_ELLIPSIS = re.compile(r"\.{3,}|\[\.\.\.\]|" + chr(0x2026))  # "..." or the single-character ellipsis
_STATUS_RANK = {"unverified": 0, "partial": 1, "verified": 2, "none": 3}

QuoteCounts = dict[str, int]


def normalize_words(text: str) -> list[str]:
    """Case-, punctuation- and Unicode-insensitive word list."""
    return words(unicodedata.normalize("NFKC", text).translate(_PUNCT_MAP).lower())


def _ngram_hashes(words: list[str]) -> set[int]:
    return {hash(" ".join(words[i : i + _NGRAM])) for i in range(len(words) - _NGRAM + 1)}


class QuoteVerifier:
    """Checks whether quoted text really occurs in one of the given sources."""

    def __init__(self, sources: Mapping[str, str]) -> None:
        self._joined: dict[str, str] = {}
        self._grams: dict[str, set[int]] = {}
        for source_id, text in sources.items():
            words = normalize_words(text)
            self._joined[source_id] = f" {' '.join(words)} "
            self._grams[source_id] = _ngram_hashes(words)

    def check(self, quote: str, only: set[str] | None = None) -> QuoteCheck:
        """Verify ``quote``; an elided quote ("A ... B") must match fragment by fragment."""
        fragments = [f for f in _ELLIPSIS.split(quote) if len(normalize_words(f)) >= _NGRAM]
        if len(fragments) < 2:
            return self._check_fragment(quote, only)
        checks = [self._check_fragment(fragment, only) for fragment in fragments]
        worst = min(checks, key=lambda c: (_STATUS_RANK[c.status], c.coverage))
        return worst.model_copy(update={"text": quote})

    def _check_fragment(self, quote: str, only: set[str] | None) -> QuoteCheck:
        words = normalize_words(quote)
        if not words:
            return QuoteCheck(text=quote, status="none", coverage=0.0)
        candidates = [s for s in self._joined if only is None or s in only]
        needle = f" {' '.join(words)} "
        for source_id in candidates:
            if needle in self._joined[source_id]:
                return QuoteCheck(text=quote, status="verified", coverage=1.0, source_id=source_id)
        if len(words) < _NGRAM:
            return QuoteCheck(text=quote, status="unverified", coverage=0.0)

        grams = _ngram_hashes(words)
        best_id, best = None, 0.0
        for source_id in candidates:
            coverage = len(grams & self._grams[source_id]) / len(grams)
            if coverage > best:
                best_id, best = source_id, coverage
        status: QuoteStatus
        if best >= VERIFIED_AT:
            status = "verified"
        elif best >= PARTIAL_AT:
            status = "partial"
        else:
            status, best_id = "unverified", None
        return QuoteCheck(text=quote, status=status, coverage=round(best, 2), source_id=best_id)


def extract_citations(text: str) -> list[str]:
    """Ordered, de-duplicated source ids cited as ``[S1]`` / ``[S1, S3]``."""
    seen: dict[str, None] = {}
    for group in _CITATION.findall(text):
        for source_id in re.split(r"\s*[,;]\s*", group):
            seen.setdefault(source_id.strip(), None)
    return list(seen)


def grounding_report(answer: str, sources: Mapping[str, str], max_quotes: int = 12) -> GroundingReport:
    """Validate the citations and quotes of a chat answer against the provided sources."""
    cited = extract_citations(answer)
    invalid = [c for c in cited if c not in sources]
    verifier = QuoteVerifier(sources)
    checks: list[QuoteCheck] = []

    for match in _QUOTED.finditer(answer):
        quote = (match.group(1) or match.group(2)).strip()
        if len(quote.split()) < 4:
            continue
        nearby = set(extract_citations(answer[match.end() : match.end() + _CITATION_WINDOW]))
        claimed = {c for c in nearby if c in sources}
        check = verifier.check(quote)
        if claimed or check.status == "verified":
            # Quote attributed to (or found in) the documents: it must hold up.
            checks.append(check)
        if len(checks) >= max_quotes:
            break

    verdict: GroundingVerdict
    if not cited and not checks:
        verdict = "general_knowledge"
    elif invalid or any(c.status == "unverified" for c in checks):
        verdict = "unverified"
    elif any(c.status == "partial" for c in checks):
        verdict = "partially_grounded"
    else:
        verdict = "grounded"
    return GroundingReport(cited_sources=cited, invalid_citations=invalid, quotes=checks, verdict=verdict)


def annotate_quotes(node: Any, verifiers: Mapping[str, QuoteVerifier]) -> QuoteCounts:
    """Walk a JSON-like result and add ``<field>_status`` next to each quote field.

    ``verifiers`` maps a field name (``quote``, ``quote_a`` ...) to the verifier to use.
    Returns counts per status for a summary badge.
    """
    counts: QuoteCounts = {"verified": 0, "partial": 0, "unverified": 0}

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for key in list(value):
                verifier = verifiers.get(key)
                if verifier is not None and isinstance(value[key], str):
                    status = verifier.check(value[key]).status if value[key].strip() else "none"
                    value[f"{key}_status"] = status
                    if status in counts:
                        counts[status] += 1
                else:
                    visit(value[key])

    visit(node)
    return counts
