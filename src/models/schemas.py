from typing import Literal

from pydantic import BaseModel, Field


class RouterDecision(BaseModel):
    """Structured output for query router.

    `complexity` (Sprint 7.6): added alongside `intent` so the router emits both
    in a single LLM call. Only meaningful when intent == "retrieval".
      - simple_lookup: single fact, single 10-K section, no formula computation
      - research_required: multi-section synthesis, calc with formula, comparative
    """

    intent: Literal["retrieval", "clarification", "out_of_scope"]
    reason: str = Field(description="Brief reason for the classification")
    complexity: Literal["simple_lookup", "research_required"] = Field(
        default="simple_lookup",
        description="Query complexity tier — drives whether to use the research agent.",
    )


class GradeResult(BaseModel):
    """Structured output for chunk relevance grading."""

    relevant: bool
    reason: str = Field(description="Why the chunk is or isn't relevant")


class HallucinationCheck(BaseModel):
    """Structured output for hallucination checking."""

    grounded: bool
    score: float = Field(ge=0.0, le=1.0, description="Confidence that answer is grounded in sources")
    explanation: str = Field(description="Explanation of grounding assessment")


class InjectionCheck(BaseModel):
    """Structured output for prompt injection detection."""

    is_injection: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


class RetrievalEvalDecision(BaseModel):
    """Structured output for selective retrieval evaluator."""

    decision: Literal["accept", "retry"] = "accept"
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    reason: str = ""


class EntityExtraction(BaseModel):
    """Structured output for entity extraction (Sprint 7a.v2).

    `company` is a lowercase slug matching the `company` payload field in Qdrant.
    `fiscal_year` is the integer year referenced in the query. Both are None when
    the query is comparative (multiple companies), generic, or doesn't specify.
    """

    company: str | None = None
    fiscal_year: int | None = None


class ChatRequest(BaseModel):
    """Request body for the chat endpoint."""

    message: str
    thread_id: str | None = None


class ChatResponse(BaseModel):
    """Response body for the chat endpoint."""

    response: str
    sources: list[dict] = []
    confidence: float | None = None
    requires_approval: bool = False
    thread_id: str | None = None


class LoginRequest(BaseModel):
    """Request body for login."""

    username: str
    password: str


class TokenResponse(BaseModel):
    """Response body for login."""

    access_token: str
    token_type: str = "bearer"
    role: str
