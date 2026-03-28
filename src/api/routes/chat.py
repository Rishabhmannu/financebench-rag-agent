import logging
import uuid

from fastapi import APIRouter, Depends
from langchain_core.messages import HumanMessage

from src.api.dependencies import get_current_user
from src.graph.builder import build_graph
from src.models.auth import User
from src.models.schemas import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat(request: ChatRequest, user: User = Depends(get_current_user)):
    """Process a chat message through the RAG agent pipeline."""
    thread_id = request.thread_id or str(uuid.uuid4())

    # Build initial state
    initial_state = {
        "messages": [HumanMessage(content=request.message)],
        "user_id": user.user_id,
        "user_role": user.role,
        "allowed_doc_types": [],
        "guardrail_status": "clean",
        "detected_pii_entities": [],
        "sanitized_query": "",
        "query_intent": "",
        "retrieved_chunks": [],
        "retrieval_query": "",
        "relevant_chunks": [],
        "grading_results": [],
        "generated_answer": "",
        "hallucination_status": "",
        "hallucination_score": 0.0,
        "requires_human_approval": False,
        "human_decision": None,
        "retrieval_retry_count": 0,
        "generation_retry_count": 0,
        "final_response": "",
        "response_metadata": {},
    }

    graph = build_graph()

    config = {
        "run_name": "rag_query",
        "tags": ["api", f"role:{user.role}"],
        "metadata": {"user_id": user.user_id, "role": user.role, "thread_id": thread_id},
    }

    result = graph.invoke(initial_state, config=config)

    metadata = result.get("response_metadata", {})
    return ChatResponse(
        response=result.get("final_response", "No response generated."),
        sources=metadata.get("sources", []),
        confidence=metadata.get("confidence"),
        requires_approval=result.get("requires_human_approval", False),
        thread_id=thread_id,
    )
