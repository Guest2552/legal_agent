"""Chunking, hybrid retrieval and the chat helpers that feed them."""

from __future__ import annotations

import numpy as np
import pytest

from app.schemas import ChatTurn
from app.services.chat import build_messages, conversation_title, retrieval_query
from app.services.chunking import chunk_segments, detect_heading, split_long_line
from app.services.parsers import Segment
from app.services.retrieval import BM25Index, HybridRetriever, reciprocal_rank_fusion, stem, tokenize
from tests.conftest import fake_vector


class TestChunking:
    @pytest.mark.parametrize(
        "line",
        ["12. TERMINATION", "ARTICLE IV - Payment Terms", "Section 5 Confidentiality", "SECURITY DEPOSIT", "(a) Scope"],
    )
    def test_detects_headings(self, line: str) -> None:
        assert detect_heading(line) == line

    @pytest.mark.parametrize(
        "line",
        [
            "The Tenant shall pay the rent on or before the fifth day of each and every month.",
            "payment,",
            "",
            "x" * 120,
        ],
    )
    def test_ignores_body_text(self, line: str) -> None:
        assert detect_heading(line) is None

    def test_chunks_respect_size_overlap_location_and_section(self) -> None:
        body = "\n".join(f"Sentence number {i} about the deposit refund timeline." for i in range(60))
        segments = [Segment(f"4. SECURITY DEPOSIT\n{body}", "Page 3"), Segment("5. TERMINATION\nShort.", "Page 4")]
        chunks = chunk_segments("doc", "lease.pdf", segments, size=400, overlap=80)

        assert all(len(c.text) <= 400 + 60 for c in chunks)
        page3 = [c for c in chunks if c.location == "Page 3"]
        assert len(page3) > 1
        assert all(c.section == "4. SECURITY DEPOSIT" for c in page3)
        last_line = page3[0].text.split("\n")[-1]
        assert page3[1].text.startswith(last_line)  # overlap carried forward
        assert chunks[-1].location == "Page 4"
        assert chunks[-1].section == "5. TERMINATION"
        assert [c.index for c in chunks] == list(range(len(chunks)))
        assert "lease.pdf / 5. TERMINATION" in chunks[-1].embedding_text

    def test_long_line_is_split_on_sentences(self) -> None:
        line = " ".join(f"Clause {i} says something important." for i in range(40))
        pieces = split_long_line(line, 200)
        assert all(len(p) <= 200 for p in pieces)
        assert " ".join(pieces) == line


class TestRetrieval:
    def test_stemming_matches_word_families(self) -> None:
        assert stem("terminated") == stem("termination") == stem("terminates")
        assert "not" in tokenize("The tenant shall not sub-let")  # legally meaningful words kept

    def test_tokenizer_handles_indic_scripts(self) -> None:
        assert tokenize("किराया समझौता") == ["किराया", "समझौता"]

    def test_bm25_ranks_relevant_document_first(self) -> None:
        corpus = [
            tokenize(t) for t in ["rent is due monthly", "security deposit refund in 30 days", "pets not allowed"]
        ]
        scores = BM25Index(corpus).scores(tokenize("when is my deposit refunded"))
        assert int(np.argmax(scores)) == 1
        assert scores[2] == 0

    def test_reciprocal_rank_fusion_rewards_agreement(self) -> None:
        fused = dict(reciprocal_rank_fusion([[1, 2, 3], [2, 1, 4]]))
        assert fused[1] == fused[2] > fused[3]
        assert fused[3] == pytest.approx(1 / 63)

    def _chunks(self) -> list:
        segments = [
            Segment("The rent is Rs. 25,000 per month.", "Page 1"),
            Segment("The landlord may enter the flat after 24 hours notice.", "Page 2"),
            Segment("Deposit of Rs. 50,000 is refundable within 30 days.", "Page 3"),
        ]
        return chunk_segments("a", "lease.txt", segments) + chunk_segments(
            "b", "policy.txt", [Segment("Pets are not allowed in the society.", "Page 1")]
        )

    def test_hybrid_search_uses_both_signals(self) -> None:
        chunks = self._chunks()
        vectors = np.vstack([fake_vector(c.embedding_text) for c in chunks])
        retriever = HybridRetriever(chunks, vectors)
        results = retriever.search("deposit refundable", fake_vector("deposit refundable"), top_k=1)
        assert results[0][0].location == "Page 3"
        assert retriever.has_vectors

    def test_keyword_only_when_no_vectors(self) -> None:
        retriever = HybridRetriever(self._chunks(), None)
        assert not retriever.has_vectors
        assert retriever.search("landlord enter", None, top_k=1)[0][0].location == "Page 2"

    def test_diversify_includes_every_document(self) -> None:
        retriever = HybridRetriever(self._chunks(), None)
        results = retriever.search("rent deposit landlord pets", None, top_k=2, diversify=True)
        assert {chunk.doc_id for chunk, _ in results} == {"a", "b"}

    def test_empty_index(self) -> None:
        assert HybridRetriever([], None).search("anything") == []


class TestChatHelpers:
    def test_short_follow_up_is_expanded_with_previous_question(self) -> None:
        history = [
            ChatTurn(role="user", content="What is the notice period?"),
            ChatTurn(role="assistant", content="1 month"),
        ]
        assert retrieval_query("and the deposit?", history) == "What is the notice period?\nand the deposit?"

    def test_long_question_is_used_as_is(self) -> None:
        question = "What happens to my deposit if I leave during the lock-in period?"
        assert retrieval_query(question, [ChatTurn(role="user", content="earlier")]) == question

    def test_conversation_title_is_short_and_single_line(self) -> None:
        assert conversation_title("  Can my landlord\nkeep the deposit?  ") == "Can my landlord keep the deposit?"
        long_title = conversation_title("word " * 40)
        assert len(long_title) == 60
        assert long_title.endswith("...")

    def test_build_messages_normalises_history(self) -> None:
        history = [
            ChatTurn(role="assistant", content="Welcome!"),  # leading model turn dropped
            ChatTurn(role="user", content="Q1"),
            ChatTurn(role="user", content="Q1 again"),  # merged
            ChatTurn(role="assistant", content="A1"),
            ChatTurn(role="user", content="unanswered"),  # dangling user turn dropped
        ]
        messages = build_messages(history, "final question")
        assert [(m.role, m.text) for m in messages] == [
            ("user", "Q1\n\nQ1 again"),
            ("model", "A1"),
            ("user", "final question"),
        ]
