"""Unicode-aware word matching shared by retrieval and quote verification.

Python's ``\\w`` does not match combining marks, so Devanagari, Tamil and other Indic
words ("किराया") would be split at every vowel sign. We extend the class with every
combining-mark code point in the Basic Multilingual Plane, computed once at import.
"""

from __future__ import annotations

import re
import unicodedata


def _combining_mark_class() -> str:
    ranges: list[tuple[int, int]] = []
    for code_point in range(0x0300, 0x10000):
        if not unicodedata.category(chr(code_point)).startswith("M"):
            continue
        if ranges and ranges[-1][1] == code_point - 1:
            ranges[-1] = (ranges[-1][0], code_point)
        else:
            ranges.append((code_point, code_point))
    return "".join(
        re.escape(chr(start)) if start == end else f"{re.escape(chr(start))}-{re.escape(chr(end))}"
        for start, end in ranges
    )


WORD_RE = re.compile(rf"[\w{_combining_mark_class()}]+")


def words(text: str) -> list[str]:
    """Split ``text`` into words, keeping Indic vowel signs attached to their letters."""
    return WORD_RE.findall(text)
