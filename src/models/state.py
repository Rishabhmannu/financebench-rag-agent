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

    # --- Retrieval ---
    retrieved_chunks: list[dict]
    retrieval_query: str

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
