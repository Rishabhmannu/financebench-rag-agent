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
    """Response body for login.

    Sprint 9 frontend handoff: includes the full identity tuple so the UI
    header can render `name (role)` and the department subtitle on first
    paint — saves a separate `/auth/me` round-trip on the login redirect.
    The contract is additive (no fields removed) so existing Gradio client
    keeps working.
    """

    access_token: str
    token_type: str = "bearer"
    user_id: str
    name: str
    role: str
    department: str


class UserPermissions(BaseModel):
    """Role-derived RBAC permissions surfaced on `/auth/me`."""

    allowed_doc_types: list[str]
    allowed_confidentiality: list[str]
    max_results: int
    requires_hitl_above: int | None


class UserMeResponse(BaseModel):
    """`GET /auth/me` response. Validates the JWT is still good and lets
    the frontend re-fetch user state after a refresh.
    """

    user_id: str
    name: str
    role: str
    department: str
    permissions: UserPermissions


class Role(BaseModel):
    """A single RBAC role record. Backs `/admin/roles` responses and the
    `is_system` flag prevents deletion of built-in roles via the API.
    """

    name: str
    allowed_doc_types: list[str]
    allowed_confidentiality: list[str]
    max_results: int
    requires_hitl_above: int | None = None
    is_system: bool = False


class RoleCreate(BaseModel):
    """Body for `POST /admin/roles`. `is_system` is intentionally absent —
    system roles are only seeded by migrations, never created via the API.
    """

    name: str
    allowed_doc_types: list[str]
    allowed_confidentiality: list[str]
    max_results: int = 10
    requires_hitl_above: int | None = None


class RoleUpdate(BaseModel):
    """Body for `PATCH /admin/roles/{name}`. All fields optional — only
    those present in the payload are updated.
    """

    allowed_doc_types: list[str] | None = None
    allowed_confidentiality: list[str] | None = None
    max_results: int | None = None
    requires_hitl_above: int | None = None
