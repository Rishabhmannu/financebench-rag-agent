from typing import Literal

from pydantic import BaseModel, Field


class RouterDecision(BaseModel):
    """Structured output for query router."""

    intent: Literal["retrieval", "clarification", "out_of_scope"]
    reason: str = Field(description="Brief reason for the classification")


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


class EntityExtraction(BaseModel):
    """Structured output for entity extraction (Sprint 7a.v2).

    `company` is a lowercase slug matching the `company` payload field in Qdrant.
    `fiscal_year` is the integer year referenced in the query. Both are None when
    the query is comparative (multiple companies), generic, or doesn't specify.
    """

    company: Literal["apple", "microsoft", "tesla"] | None = None
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
