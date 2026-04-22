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

    # --- Grading ---
    relevant_chunks: list[dict]
    grading_results: list[dict]

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
