"""Pydantic models: API requests/responses and the structured outputs we ask Gemini for.

The ``*Result`` / ``*Report`` models double as Gemini ``response_schema`` definitions, so
they deliberately use only simple types (str, int, list, Literal) that the API supports.
Every ``quote`` field is later checked against the source text by
:mod:`app.services.grounding`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# Display name -> BCP-47 tag (used by the browser for speech recognition / synthesis).
LANGUAGES: dict[str, str] = {
    "English": "en-IN",
    "Hindi": "hi-IN",
    "Tamil": "ta-IN",
    "Telugu": "te-IN",
    "Kannada": "kn-IN",
    "Malayalam": "ml-IN",
    "Bengali": "bn-IN",
    "Marathi": "mr-IN",
    "Gujarati": "gu-IN",
    "Punjabi": "pa-IN",
    "Urdu": "ur-IN",
    "Odia": "or-IN",
    "Spanish": "es-ES",
    "French": "fr-FR",
    "German": "de-DE",
    "Portuguese": "pt-BR",
    "Arabic": "ar-SA",
    "Chinese (Simplified)": "zh-CN",
    "Japanese": "ja-JP",
}

JURISDICTIONS: tuple[str, ...] = (
    "Auto-detect",
    "India",
    "United States",
    "United Kingdom",
    "European Union",
    "Canada",
    "Australia",
    "Singapore",
    "United Arab Emirates",
    "Other / International",
)

RiskLevel = Literal["high", "medium", "low"]
ReadingLevel = Literal["simple", "standard", "detailed"]
QuoteStatus = Literal["verified", "partial", "unverified", "none"]
GroundingVerdict = Literal["grounded", "partially_grounded", "general_knowledge", "unverified"]

# --------------------------------------------------------------------------------------
# Shared request options
# --------------------------------------------------------------------------------------


class AssistantOptions(BaseModel):
    """Personalisation shared by every AI endpoint."""

    language: str = "English"
    jurisdiction: str = "Auto-detect"
    user_role: str = Field(default="", max_length=80)

    @field_validator("language")
    @classmethod
    def _known_language(cls, value: str) -> str:
        if value not in LANGUAGES:
            raise ValueError("unsupported language")
        return value

    @field_validator("jurisdiction")
    @classmethod
    def _known_jurisdiction(cls, value: str) -> str:
        if value not in JURISDICTIONS:
            raise ValueError("unsupported jurisdiction")
        return value

    @field_validator("user_role")
    @classmethod
    def _clean_role(cls, value: str) -> str:
        return " ".join(value.split())


# --------------------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------------------


class DocumentInfo(BaseModel):
    id: str
    name: str
    kind: str
    size_bytes: int
    characters: int
    chunks: int
    locations: int
    semantic_search: bool
    notes: list[str]
    created_at: float


class DocumentText(BaseModel):
    id: str
    name: str
    sections: list[dict[str, str]]
    truncated: bool


class PasteTextRequest(BaseModel):
    name: str = Field(default="Pasted text", min_length=1, max_length=120)
    text: str = Field(min_length=20, max_length=500_000)


# --------------------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------------------


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class ChatRequest(AssistantOptions):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=64)
    document_ids: list[str] | None = Field(default=None, max_length=20)
    voice: bool = False
    web_search: bool = True


class ConversationSummary(BaseModel):
    id: str
    title: str
    created_at: float
    updated_at: float
    message_count: int


class MessageOut(BaseModel):
    id: int
    role: Literal["user", "assistant"]
    content: str
    meta: dict[str, Any]
    created_at: float


class ConversationDetail(BaseModel):
    id: str
    title: str
    messages: list[MessageOut]


class RenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class SourceRef(BaseModel):
    id: str
    doc_id: str
    doc_name: str
    location: str
    section: str
    text: str


class QuoteCheck(BaseModel):
    text: str
    status: QuoteStatus
    coverage: float
    source_id: str | None = None


class GroundingReport(BaseModel):
    cited_sources: list[str]
    invalid_citations: list[str]
    quotes: list[QuoteCheck]
    verdict: GroundingVerdict


class WebSource(BaseModel):
    title: str
    uri: str


# --------------------------------------------------------------------------------------
# Analysis requests
# --------------------------------------------------------------------------------------


class SimplifyRequest(AssistantOptions):
    document_id: str
    level: ReadingLevel = "standard"


class RiskRequest(AssistantOptions):
    document_id: str


class CompareRequest(AssistantOptions):
    document_a: str
    document_b: str


class SituationRequest(AssistantOptions):
    situation: str = Field(default="", max_length=6000)
    document_ids: list[str] = Field(default_factory=list, max_length=20)


class ExportRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    markdown: str = Field(min_length=1, max_length=300_000)


# --------------------------------------------------------------------------------------
# Structured outputs requested from Gemini
# --------------------------------------------------------------------------------------


class Party(BaseModel):
    name: str
    role: str


class KeyPoint(BaseModel):
    point: str
    quote: str
    location: str


class TermExplained(BaseModel):
    term: str
    meaning: str


class SectionExplained(BaseModel):
    heading: str
    explanation: str
    location: str


class SimplifyResult(BaseModel):
    document_type: str
    one_line_summary: str
    parties: list[Party]
    plain_summary: str
    key_points: list[KeyPoint]
    sections: list[SectionExplained]
    terms_explained: list[TermExplained]


class ClauseFinding(BaseModel):
    category: str
    title: str
    risk: RiskLevel
    explanation: str
    quote: str
    location: str
    suggestion: str


class Obligation(BaseModel):
    party: str
    obligation: str
    deadline: str
    quote: str


class KeyDate(BaseModel):
    date: str
    event: str
    quote: str


class RedFlag(BaseModel):
    issue: str
    why_it_matters: str
    quote: str


class Inconsistency(BaseModel):
    issue: str
    quote: str
    conflicting_quote: str


class MissingClause(BaseModel):
    clause: str
    why_it_matters: str


class RiskReport(BaseModel):
    overall_risk: RiskLevel
    risk_score: int = Field(ge=0, le=100)
    verdict: str
    favours: str
    clauses: list[ClauseFinding]
    obligations: list[Obligation]
    key_dates: list[KeyDate]
    red_flags: list[RedFlag]
    inconsistencies: list[Inconsistency]
    missing_clauses: list[MissingClause]


class Difference(BaseModel):
    topic: str
    doc_a: str
    doc_b: str
    quote_a: str
    quote_b: str
    impact: str
    favours: Literal["A", "B", "neutral"]
    severity: RiskLevel


class UniqueClause(BaseModel):
    clause: str
    quote: str
    impact: str


class ComparisonReport(BaseModel):
    summary: str
    better_for_user: Literal["A", "B", "equal"]
    reason: str
    differences: list[Difference]
    only_in_a: list[UniqueClause]
    only_in_b: list[UniqueClause]
    negotiation_points: list[str]


class ActionOption(BaseModel):
    option: str
    pros: list[str]
    cons: list[str]
    effort: str
    best_when: str


class ChecklistItem(BaseModel):
    task: str
    why: str
    deadline: str
    priority: RiskLevel
    quote: str


class Deadline(BaseModel):
    date: str
    what: str
    consequence: str
    quote: str


class LegalBasis(BaseModel):
    law: str
    relevance: str


class ActionPlan(BaseModel):
    situation_summary: str
    legal_position: str
    legal_basis: list[LegalBasis]
    options: list[ActionOption]
    checklist: list[ChecklistItem]
    deadlines: list[Deadline]
    documents_to_gather: list[str]


class TimelineEvent(BaseModel):
    date: str
    event: str
    quote: str


class LawyerQuestion(BaseModel):
    question: str
    why: str


class LawyerBrief(BaseModel):
    case_summary: str
    goal: str
    timeline: list[TimelineEvent]
    key_facts: list[str]
    legal_issues: list[str]
    questions_to_ask: list[LawyerQuestion]
    documents_to_bring: list[str]
    open_points: list[str]
