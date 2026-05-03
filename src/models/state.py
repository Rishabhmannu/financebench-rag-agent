from typing import Annotated, Optional, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class RAGState(TypedDict):
    """Shared state flowing through every node in the RAG agent graph."""

    # --- Input ---
    messages: Annotated[list[BaseMessage], add_messages]

    # --- Auth ---
    user_id: str
    user_role: str  # "finance", "hr", "c_level", "analyst", "admin"
    allowed_doc_types: list[str]  # ["10k", "invoice", "expense_policy", ...]

    # --- Guardrails ---
    guardrail_status: str  # "clean", "pii_detected", "injection_detected", "out_of_scope"
    detected_pii_entities: list[dict]
    sanitized_query: str

    # --- Routing ---
    query_intent: str  # "retrieval", "clarification", "out_of_scope"
    # Complexity classification (Sprint 7.6): "simple_lookup" (single-fact retrieval,
    # fast path) vs "research_required" (multi-section synthesis, calc with formula,
    # comparative — routed to the research agent subgraph). None means classifier
    # didn't set it (treat as simple_lookup downstream).
    query_complexity: str | None

    # --- Entity extraction (Sprint 7a.v2) ---
    # Lowercase slug matching a `company` payload value in Qdrant, or None if the
    # query isn't scoped to a specific company (e.g. "compare Apple and MSFT").
    target_company: str | None
    target_fiscal_year: int | None

    # --- Retrieval ---
    retrieved_chunks: list[dict]  # Hybrid candidates (top ~50 from dense+BM25 RRF)
    retrieval_query: str

    # --- Reranking ---
    reranked_chunks: list[dict]  # Cross-encoder top-K selected from retrieved_chunks
    candidate_diagnostics: list[dict]  # Optional per-candidate validation/LTR diagnostics

    # --- Optional selective retrieval evaluator ---
    retrieval_evaluator_confidence: float | None
    retrieval_evaluator_decision: str | None  # "accept", "retry"

    # --- Grading ---
    relevant_chunks: list[dict]
    grading_results: list[dict]

    # --- Empty-context fallback diagnostics (Sprint 7.7 Day 7) ---
    # Set to True when the retrieval node had to drop its entity filter to find
    # any chunks at all (retrieval-side fallback), or when the grader had to
    # pass through top-K reranked chunks because all chunks failed grading
    # (grader-side fallback). Either signal indicates a "best-effort" answer
    # rather than a confidently-grounded one — useful for failure analysis.
    retrieval_fallback_used: bool | None
    grader_fallback_used: bool | None

    # --- Research agent (Sprint 7.6) ---
    # Set when the research agent runs (query_complexity == "research_required").
    # `agent_synthesis` is a structured-text reasoning block the agent produces
    # to guide the main generator; raw `relevant_chunks` are still populated and
    # remain the substrate the hallucination checker grounds against.
    agent_synthesis: str | None
    agent_turns_used: int | None
    agent_sub_questions: list[str] | None

    # --- Generation ---
    generated_answer: str

    # --- Hallucination Check ---
    hallucination_status: str  # "grounded", "hallucinated"
    hallucination_score: float

    # --- HITL ---
    requires_human_approval: bool
    human_decision: Optional[str]  # "approved", "rejected", None

    # --- Control Flow ---
    retrieval_retry_count: int
    generation_retry_count: int

    # --- Output ---
    final_response: str
    response_metadata: dict
