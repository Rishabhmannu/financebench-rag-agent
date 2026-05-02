import logging

from langchain_core.messages import HumanMessage

from src.config.prompts import ROUTER_PROMPT
from src.models.schemas import RouterDecision
from src.models.state import RAGState
from src.services.llm_factory import LLMFactory

logger = logging.getLogger(__name__)


def router_node(state: RAGState) -> dict:
    """Classify query intent and complexity in a single LLM call.

    Sprint 7.6: emits `query_complexity` in addition to `query_intent`.
    Complexity drives whether downstream uses the fast retrieval path or the
    research-agent subgraph (see route_after_router in edges.py).
    """
    query = state.get("sanitized_query", "")
    if not query:
        return {"query_intent": "clarification", "query_complexity": None}

    try:
        llm = LLMFactory.get_router_llm()
        structured_llm = llm.with_structured_output(RouterDecision)
        result: RouterDecision = structured_llm.invoke([HumanMessage(content=ROUTER_PROMPT.format(query=query))])
        logger.info(
            f"Router decision: intent={result.intent}, complexity={result.complexity}, reason={result.reason}"
        )
        return {
            "query_intent": result.intent,
            "query_complexity": result.complexity,
        }
    except Exception as e:
        logger.error(f"Router failed, defaulting to retrieval/simple_lookup: {e}")
        return {"query_intent": "retrieval", "query_complexity": "simple_lookup"}
