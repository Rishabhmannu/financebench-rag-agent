import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from langchain_core.messages import HumanMessage
from sse_starlette.sse import EventSourceResponse

from src.api.dependencies import get_current_user
from src.config.rbac_config import approvers_for, get_permissions
from src.graph.builder import build_graph
from src.models.auth import User
from src.models.schemas import ChatRequest, ChatResponse
from src.services.cost_tracker import RequestScopedCostHandler
from src.services.reranker_service import get_reranker

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
    # Terminal nodes — must be in this dict so their on_chain_end output is
    # captured as final_state (otherwise the SSE `final` event falls back to
    # "No response generated.").
    "no_info_response": "Compiling response",
    "blocked_response": "Compiling response",
    "out_of_scope": "Compiling response",
    "clarification": "Compiling response",
}


@router.post("", response_model=ChatResponse)
async def chat(request: ChatRequest, user: User = Depends(get_current_user), http_request: Request = None):
    """Process a chat message through the RAG agent pipeline (non-streaming)."""
    thread_id = request.thread_id or str(uuid.uuid4())
    initial_state = _build_initial_state(request.message, user)

    checkpointer = getattr(http_request.app.state, "checkpointer", None) if http_request else None
    graph = _get_graph(checkpointer=checkpointer)

    req_cost = RequestScopedCostHandler()
    config = {
        "configurable": {"thread_id": thread_id},
        "run_name": "rag_query",
        "tags": ["api", f"role:{user.role}"],
        "metadata": {"user_id": user.user_id, "role": user.role, "thread_id": thread_id, "hitl_enabled": checkpointer is not None},
        "callbacks": [req_cost],
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
    cost_summary = req_cost.aggregate()
    return ChatResponse(
        response=result.get("final_response", "No response generated."),
        sources=metadata.get("sources", []),
        confidence=metadata.get("confidence"),
        requires_approval=result.get("requires_human_approval", False),
        thread_id=thread_id,
        cost_usd=cost_summary["cost_usd"],
        tokens=cost_summary["tokens"],
    )


@router.post("/stream")
async def chat_stream(request: ChatRequest, user: User = Depends(get_current_user), http_request: Request = None):
    """Process a chat message with SSE streaming of progress and tokens."""
    thread_id = request.thread_id or str(uuid.uuid4())
    initial_state = _build_initial_state(request.message, user)

    checkpointer = getattr(http_request.app.state, "checkpointer", None) if http_request else None
    graph = _get_graph(checkpointer=checkpointer)

    req_cost = RequestScopedCostHandler()
    config = {
        "configurable": {"thread_id": thread_id},
        "run_name": "rag_query_stream",
        "tags": ["api", "streaming", f"role:{user.role}"],
        "metadata": {"user_id": user.user_id, "role": user.role, "thread_id": thread_id, "hitl_enabled": checkpointer is not None},
        "callbacks": [req_cost],
    }

    # Phase 3.5: if the requester's role has a HITL threshold, suppress token
    # streaming entirely. The requester must never see the draft answer; they
    # only see the answer after an authorized approver releases it (via
    # /v1/hitl/approve). For roles without a threshold (admin, analyst, hr),
    # streaming proceeds normally.
    suppress_tokens = get_permissions(user.role).get("requires_hitl_above") is not None

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

                # LLM token streaming — only from the generator node, and only
                # when the requester's role has no HITL threshold (otherwise
                # we'd be streaming a draft the requester isn't cleared to see).
                elif kind == "on_chat_model_stream" and not suppress_tokens:
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        tags = event.get("tags", [])
                        metadata = event.get("metadata", {}) or {}
                        node_name = metadata.get("langgraph_node", "")
                        if (
                            "generator" in name
                            or any("generator" in t for t in tags)
                            or "generator" in node_name
                        ):
                            yield json.dumps({
                                "type": "token",
                                "content": chunk.content,
                            })

            # After stream ends, check for HITL interrupts via graph state.
            # Phase 3.5: emit pending_approval with the list of roles authorized
            # to approve — the requester's CLI uses this to display "waiting
            # for approval by X|Y". The draft answer is intentionally NOT
            # included; only approvers see it via /v1/approvals/{thread_id}.
            if checkpointer is not None:
                try:
                    graph_state = await graph.aget_state(config)
                    if graph_state.tasks:
                        for task in graph_state.tasks:
                            if hasattr(task, "interrupts") and task.interrupts:
                                interrupt_value = task.interrupts[0].value
                                yield json.dumps({
                                    "type": "pending_approval",
                                    "reason": interrupt_value.get("reason", "Approval required"),
                                    "max_amount": interrupt_value.get("max_amount"),
                                    "threshold": interrupt_value.get("threshold"),
                                    "thread_id": thread_id,
                                    "approvers": approvers_for(user.role),
                                    "requester_role": user.role,
                                })
                                return
                except Exception as e:
                    logger.warning(f"Failed to check graph state for interrupts: {e}")

            # Normal completion — emit final event
            cost_summary = req_cost.aggregate()
            if final_state:
                metadata = final_state.get("response_metadata", {})
                yield json.dumps({
                    "type": "final",
                    "response": final_state.get("final_response", ""),
                    "sources": metadata.get("sources", []),
                    "confidence": metadata.get("confidence"),
                    "requires_approval": final_state.get("requires_human_approval", False),
                    "thread_id": thread_id,
                    "cost_usd": cost_summary["cost_usd"],
                    "tokens": cost_summary["tokens"],
                })
            else:
                yield json.dumps({
                    "type": "final",
                    "response": "No response generated.",
                    "sources": [],
                    "confidence": None,
                    "requires_approval": False,
                    "thread_id": thread_id,
                    "cost_usd": cost_summary["cost_usd"],
                    "tokens": cost_summary["tokens"],
                })

        except Exception as e:
            logger.error(f"Streaming graph execution failed: {e}", exc_info=True)
            yield json.dumps({
                "type": "error",
                "message": "An error occurred processing your request.",
            })

    return EventSourceResponse(event_generator())


@router.get("/result/{thread_id}")
async def chat_result(
    thread_id: str,
    http_request: Request,
    user: User = Depends(get_current_user),
):
    """Phase 3.5: the requester polls this to retrieve the answer once their
    HITL-paused query has been approved or rejected. Returns status=pending
    while paused; status=approved/rejected with the response once decided.

    Ownership-gated: only the original requester (or admin) can fetch.
    """
    checkpointer = getattr(http_request.app.state, "checkpointer", None)
    if checkpointer is None:
        raise HTTPException(status_code=503, detail="Result polling unavailable (no checkpointer)")

    from src.services.thread_service import get_thread_owner_role
    pool = getattr(http_request.app.state, "pool", None)
    if pool is not None:
        owner, requester_role = await get_thread_owner_role(pool, thread_id)
        if owner is None:
            raise HTTPException(status_code=404, detail="Thread not found")
        if owner != user.user_id and user.role != "admin":
            raise HTTPException(status_code=403, detail="Thread belongs to a different user")

    graph = _get_graph(checkpointer=checkpointer)
    cfg = {"configurable": {"thread_id": thread_id}}
    gs = await graph.aget_state(cfg)
    if gs is None or not gs.values:
        return {"status": "pending", "thread_id": thread_id}

    for task in (gs.tasks or []):
        if getattr(task, "interrupts", None):
            v = task.interrupts[0].value
            return {
                "status": "pending",
                "thread_id": thread_id,
                "reason": (v or {}).get("reason"),
                "max_amount": (v or {}).get("max_amount"),
                "threshold": (v or {}).get("threshold"),
            }

    values = gs.values or {}
    final_response = values.get("final_response", "")
    metadata = values.get("response_metadata") or {}
    requires_approval = values.get("requires_human_approval", False)
    human_decision = values.get("human_decision")

    if requires_approval and human_decision == "rejected":
        return {
            "status": "rejected",
            "thread_id": thread_id,
            "response": final_response,
        }

    return {
        "status": "approved" if requires_approval else "ready",
        "thread_id": thread_id,
        "response": final_response,
        "sources": metadata.get("sources", []),
        "confidence": metadata.get("confidence"),
    }
