"""Structured document analysis: simplify, risk scan, compare, action plan, lawyer brief.

Analysis tools read the *whole* document (Gemini's long context) rather than retrieved
snippets, because completeness matters more than precision for these tasks. Every quote
in the structured result is then verified against the source text.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from app import prompts
from app.config import Settings
from app.db.repositories import AnalysisRepository
from app.schemas import (
    ActionPlan,
    AssistantOptions,
    ComparisonReport,
    LawyerBrief,
    RiskReport,
    SimplifyResult,
)
from app.services.documents import StoredDocument
from app.services.grounding import QuoteCounts, QuoteVerifier, annotate_quotes
from app.services.llm import LLMClient

AnalysisPayload = dict[str, Any]


class AnalysisService:
    """Runs structured analyses; results are cached per session in SQLite."""

    def __init__(self, llm: LLMClient, cache: AnalysisRepository, settings: Settings) -> None:
        self._llm = llm
        self._cache = cache
        self._settings = settings

    # -- public tasks -------------------------------------------------------------------

    async def simplify(
        self, session_id: str, doc: StoredDocument, options: AssistantOptions, level: str
    ) -> AnalysisPayload:
        task = prompts.SIMPLIFY_TASK.format(level=prompts.READING_LEVELS[level])
        return await self._run(session_id, "simplify", SimplifyResult, task, [doc], options, extra=level)

    async def risks(self, session_id: str, doc: StoredDocument, options: AssistantOptions) -> AnalysisPayload:
        task = prompts.RISK_TASK.format(perspective=prompts.perspective(options))
        return await self._run(session_id, "risks", RiskReport, task, [doc], options)

    async def compare(
        self, session_id: str, doc_a: StoredDocument, doc_b: StoredDocument, options: AssistantOptions
    ) -> AnalysisPayload:
        task = prompts.COMPARE_TASK.format(perspective=prompts.perspective(options))
        return await self._run(session_id, "compare", ComparisonReport, task, [doc_a, doc_b], options, labels="AB")

    async def action_plan(
        self, session_id: str, docs: Sequence[StoredDocument], options: AssistantOptions, situation: str
    ) -> AnalysisPayload:
        task = prompts.ACTION_PLAN_TASK
        return await self._run(session_id, "action_plan", ActionPlan, task, docs, options, situation=situation)

    async def lawyer_brief(
        self, session_id: str, docs: Sequence[StoredDocument], options: AssistantOptions, situation: str
    ) -> AnalysisPayload:
        return await self._run(session_id, "brief", LawyerBrief, prompts.BRIEF_TASK, docs, options, situation=situation)

    # -- pipeline -------------------------------------------------------------------------

    async def _run(
        self,
        session_id: str,
        name: str,
        schema: type[BaseModel],
        task: str,
        docs: Sequence[StoredDocument],
        options: AssistantOptions,
        *,
        situation: str = "",
        extra: str = "",
        labels: str = "",
    ) -> AnalysisPayload:
        key = self._cache_key(name, docs, options, situation, extra)
        documents = [{"id": d.id, "name": d.name} for d in docs]
        cached = await self._cache.get(session_id, key)
        if cached is not None:
            return {**cached, "documents": documents}

        budget = self._settings.analysis_max_chars // max(len(docs), 1)
        wrapped: list[str] = []
        truncated = False
        for index, doc in enumerate(docs):
            text, cut = doc.marked_text(budget)
            truncated = truncated or cut
            label = labels[index] if index < len(labels) else str(index + 1)
            wrapped.append(prompts.format_document(label, doc.name, text))

        result = await self._llm.generate_json(
            system=prompts.analysis_system_prompt(options),
            prompt=prompts.analysis_prompt(task, wrapped, situation),
            schema=schema,
        )
        data = result.model_dump()
        payload: AnalysisPayload = {
            "result": data,
            "quote_check": _verify(data, docs, compare=bool(labels)),
            "truncated": truncated,
        }
        await self._cache.put(session_id, key, payload)
        return {**payload, "documents": documents}

    def _cache_key(
        self,
        name: str,
        docs: Sequence[StoredDocument],
        options: AssistantOptions,
        situation: str,
        extra: str,
    ) -> str:
        material = json.dumps(
            {
                "task": name,
                "model": self._settings.gemini_analysis_model,
                "docs": [d.sha256 for d in docs],
                "options": options.model_dump(include={"language", "jurisdiction", "user_role"}),
                "situation": situation.strip(),
                "extra": extra,
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode()).hexdigest()


def _verify(data: dict[str, Any], docs: Sequence[StoredDocument], *, compare: bool) -> QuoteCounts:
    """Annotate every quote in ``data`` with its verification status."""
    if compare:
        verifier_a = QuoteVerifier({"A": docs[0].full_text})
        verifier_b = QuoteVerifier({"B": docs[1].full_text})
        totals = annotate_quotes(data["differences"], {"quote_a": verifier_a, "quote_b": verifier_b})
        for part, verifier in (("only_in_a", verifier_a), ("only_in_b", verifier_b)):
            for status, count in annotate_quotes(data[part], {"quote": verifier}).items():
                totals[status] += count
        return totals
    verifier = QuoteVerifier({d.id: d.full_text for d in docs})
    return annotate_quotes(data, {"quote": verifier, "conflicting_quote": verifier})
