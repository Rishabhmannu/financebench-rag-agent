import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from langchain_core.messages import HumanMessage
from sse_starlette.sse import EventSourceResponse

from src.api.dependencies import get_current_user
from src.graph.builder import build_graph
from src.models.auth import User
from src.models.schemas import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

# Cache compiled graphs keyed by checkpointer identity to avoid recompilation
_graphs: dict = {}


def _get_graph(checkpointer=None):
    key = id(checkpointer)
    if key not in _graphs:
        _graphs[key] = build_graph(checkpointer=checkpointer)
    return _graphs[key]


def _build_initial_state(message: str, user: User) -> dict:
    """Build the initial RAGState for a chat request."""
    return {
        "messages": [HumanMessage(content=message)],
        "user_id": user.user_id,
        "user_role": user.role,
        "allowed_doc_types": [],
        "guardrail_status": "clean",
        "detected_pii_entities": [],
        "sanitized_query": "",
        "query_intent": "",
        "target_company": None,
        "target_fiscal_year": None,
        "retrieved_chunks": [],
        "reranked_chunks": [],
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


# Human-readable node labels for streaming progress
_NODE_LABELS = {
    "rbac_gate": "Checking permissions",
    "guardrails": "Running safety checks",
    "entity_extractor": "Identifying target company",
    "router": "Classifying query",
    "retrieval": "Searching documents",
    "reranker": "Reranking candidates",
    "grader": "Evaluating relevance",
    "query_rewriter": "Refining search query",
    "generator": "Generating answer",
    "hallucination_checker": "Verifying accuracy",
    "hitl_gate": "Checking approval requirements",
    "response_formatter": "Formatting response",
}


@router.post("", response_model=ChatResponse)
async def chat(request: ChatRequest, user: User = Depends(get_current_user), http_request: Request = None):
    """Process a chat message through the RAG agent pipeline (non-streaming)."""
    thread_id = request.thread_id or str(uuid.uuid4())
    initial_state = _build_initial_state(request.message, user)

    checkpointer = getattr(http_request.app.state, "checkpointer", None) if http_request else None
    graph = _get_graph(checkpointer=checkpointer)

    config = {
        "configurable": {"thread_id": thread_id},
        "run_name": "rag_query",
        "tags": ["api", f"role:{user.role}"],
        "metadata": {"user_id": user.user_id, "role": user.role, "thread_id": thread_id, "hitl_enabled": checkpointer is not None},
    }

    try:
        result = await graph.ainvoke(initial_state, config=config)
    except Exception as e:
        logger.error(f"Graph execution failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="An error occurred processing your request.")

    # Check if graph was interrupted by HITL (via graph state, not return values)
    if checkpointer is not None:
        try:
            graph_state = await graph.aget_state(config)
            if graph_state.tasks:
                for task in graph_state.tasks:
                    if hasattr(task, "interrupts") and task.interrupts:
                        interrupt_value = task.interrupts[0].value
                        return ChatResponse(
                            response=interrupt_value.get("answer_preview", ""),
                            sources=[],
                            confidence=None,
                            requires_approval=True,
                            thread_id=thread_id,
                        )
        except Exception as e:
            logger.warning(f"Failed to check graph state for interrupts: {e}")

    metadata = result.get("response_metadata", {})
    return ChatResponse(
        response=result.get("final_response", "No response generated."),
        sources=metadata.get("sources", []),
        confidence=metadata.get("confidence"),
        requires_approval=result.get("requires_human_approval", False),
        thread_id=thread_id,
    )


@router.post("/stream")
async def chat_stream(request: ChatRequest, user: User = Depends(get_current_user), http_request: Request = None):
    """Process a chat message with SSE streaming of progress and tokens."""
    thread_id = request.thread_id or str(uuid.uuid4())
    initial_state = _build_initial_state(request.message, user)

    checkpointer = getattr(http_request.app.state, "checkpointer", None) if http_request else None
    graph = _get_graph(checkpointer=checkpointer)

    config = {
        "configurable": {"thread_id": thread_id},
        "run_name": "rag_query_stream",
        "tags": ["api", "streaming", f"role:{user.role}"],
        "metadata": {"user_id": user.user_id, "role": user.role, "thread_id": thread_id, "hitl_enabled": checkpointer is not None},
    }

    async def event_generator():
        final_state = None

        try:
            async for event in graph.astream_events(
                initial_state, config=config, version="v2"
            ):
                kind = event.get("event", "")
                name = event.get("name", "")

                # Node start events — emit progress
                if kind == "on_chain_start" and name in _NODE_LABELS:
                    yield json.dumps({
                        "type": "node_start",
                        "node": name,
                        "label": _NODE_LABELS[name],
                    })

                # Node end events — capture final state
                elif kind == "on_chain_end" and name in _NODE_LABELS:
                    output = event.get("data", {}).get("output")
                    if isinstance(output, dict):
                        final_state = output
                    yield json.dumps({
                        "type": "node_end",
                        "node": name,
                    })

                # LLM token streaming — only from the generator node
                elif kind == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        tags = event.get("tags", [])
                        if "generator" in name or any("generator" in t for t in tags):
                            yield json.dumps({
                                "type": "token",
                                "content": chunk.content,
                            })

            # After stream ends, check for HITL interrupts via graph state
            if checkpointer is not None:
                try:
                    graph_state = await graph.aget_state(config)
                    if graph_state.tasks:
                        for task in graph_state.tasks:
                            if hasattr(task, "interrupts") and task.interrupts:
                                interrupt_value = task.interrupts[0].value
                                yield json.dumps({
                                    "type": "hitl_interrupt",
                                    "answer_preview": interrupt_value.get("answer_preview", ""),
                                    "reason": interrupt_value.get("reason", "Approval required"),
                                    "thread_id": thread_id,
                                })
                                return
                except Exception as e:
                    logger.warning(f"Failed to check graph state for interrupts: {e}")

            # Normal completion — emit final event
            if final_state:
                metadata = final_state.get("response_metadata", {})
                yield json.dumps({
                    "type": "final",
                    "response": final_state.get("final_response", ""),
                    "sources": metadata.get("sources", []),
                    "confidence": metadata.get("confidence"),
                    "requires_approval": final_state.get("requires_human_approval", False),
                    "thread_id": thread_id,
                })
            else:
                yield json.dumps({
                    "type": "final",
                    "response": "No response generated.",
                    "sources": [],
                    "confidence": None,
                    "requires_approval": False,
                    "thread_id": thread_id,
                })

        except Exception as e:
            logger.error(f"Streaming graph execution failed: {e}", exc_info=True)
            yield json.dumps({
                "type": "error",
                "message": "An error occurred processing your request.",
            })

    return EventSourceResponse(event_generator())
