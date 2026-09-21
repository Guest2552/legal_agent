"""Hybrid retrieval: BM25 keyword search + dense embeddings, fused with Reciprocal Rank Fusion.

Keyword search catches exact legal terms, clause numbers and amounts ("Clause 7.2",
"30 days"); embeddings catch paraphrases ("kick me out" -> "terminate the tenancy").
RRF merges both rankings without needing to calibrate their score scales.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Sequence

import numpy as np

from app.services.chunking import Chunk
from app.services.textutils import words

# Deliberately tiny: words like "not", "no", "shall", "must" and "may" carry legal meaning.
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "with",
    ]
)
_SUFFIXES = (
    "ational", "ations", "ation", "ating", "ated", "ates", "ate",
    "ments", "ment", "ings", "ing", "ies", "ed", "es", "s",
)  # fmt: skip
_MIN_STEM = 4


def stem(word: str) -> str:
    """Very light suffix stripping so "terminate/terminated/termination" match."""
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= _MIN_STEM:
            word = word[: -len(suffix)]
            break
    if len(word) > _MIN_STEM and word.endswith("e"):
        word = word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    """Lower-case, Unicode-aware word tokens (works for Indic scripts too), lightly stemmed."""
    return [stem(tok) for tok in words(text.lower()) if tok not in _STOPWORDS]


class BM25Index:
    """Okapi BM25 over an inverted index."""

    def __init__(self, corpus: Sequence[Sequence[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b
        self._size = len(corpus)
        self._lengths = np.array([len(doc) for doc in corpus], dtype=np.float32)
        self._avg_length = float(self._lengths.mean()) if self._size else 0.0
        self._postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for doc_index, doc in enumerate(corpus):
            for term, freq in Counter(doc).items():
                self._postings[term].append((doc_index, freq))
        self._idf = {
            term: math.log(1 + (self._size - len(posts) + 0.5) / (len(posts) + 0.5))
            for term, posts in self._postings.items()
        }

    def scores(self, query: Sequence[str]) -> np.ndarray:
        scores = np.zeros(self._size, dtype=np.float32)
        if not self._size or not self._avg_length:
            return scores
        for term in set(query):
            idf = self._idf.get(term)
            if idf is None:
                continue
            for doc_index, freq in self._postings[term]:
                norm = self._k1 * (1 - self._b + self._b * self._lengths[doc_index] / self._avg_length)
                scores[doc_index] += idf * freq * (self._k1 + 1) / (freq + norm)
        return scores


def reciprocal_rank_fusion(rankings: Sequence[Sequence[int]], k: int = 60) -> list[tuple[int, float]]:
    """Fuse several ranked lists of item ids; returns ``(id, score)`` best first."""
    fused: dict[int, float] = defaultdict(float)
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            fused[item] += 1.0 / (k + rank + 1)
    return sorted(fused.items(), key=lambda pair: pair[1], reverse=True)


class HybridRetriever:
    """Searches a fixed set of chunks. ``vectors`` rows align with ``chunks`` (zero row = none)."""

    def __init__(self, chunks: Sequence[Chunk], vectors: np.ndarray | None = None) -> None:
        self._chunks = list(chunks)
        self._bm25 = BM25Index([tokenize(c.embedding_text) for c in self._chunks])
        self._vectors = vectors if vectors is not None and len(vectors) == len(self._chunks) else None

    @property
    def has_vectors(self) -> bool:
        return self._vectors is not None and bool(np.any(self._vectors))

    def search(
        self,
        query: str,
        query_vector: np.ndarray | None = None,
        *,
        top_k: int = 8,
        candidates: int = 40,
        diversify: bool = False,
    ) -> list[tuple[Chunk, float]]:
        """Return the best ``top_k`` chunks. With ``diversify`` every document gets >= 1 hit."""
        if not self._chunks:
            return []
        lexical = self._bm25.scores(tokenize(query))
        rankings = [[int(i) for i in np.argsort(-lexical)[:candidates] if lexical[i] > 0]]
        if query_vector is not None and self.has_vectors:
            assert self._vectors is not None
            similarity = self._vectors @ query_vector.astype(np.float32)
            rankings.append([int(i) for i in np.argsort(-similarity)[:candidates]])
        fused = reciprocal_rank_fusion(rankings)
        if not fused:  # no keyword overlap and no vectors: fall back to document order
            fused = [(i, 0.0) for i in range(min(top_k, len(self._chunks)))]

        selected = fused[:top_k]
        if diversify:
            present = {self._chunks[i].doc_id for i, _ in selected}
            for index, score in fused[top_k:]:
                doc_id = self._chunks[index].doc_id
                if doc_id not in present:
                    selected.append((index, score))
                    present.add(doc_id)
        return [(self._chunks[i], score) for i, score in selected]
