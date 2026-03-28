import logging

from langchain_core.messages import HumanMessage

from src.config.prompts import ROUTER_PROMPT
from src.models.schemas import RouterDecision
from src.models.state import RAGState
from src.services.llm_factory import LLMFactory

logger = logging.getLogger(__name__)


def router_node(state: RAGState) -> dict:
    """Classify query intent: retrieval, clarification, or out_of_scope."""
    query = state.get("sanitized_query", "")
    if not query:
        return {"query_intent": "clarification"}

    try:
        llm = LLMFactory.get_router_llm()
        structured_llm = llm.with_structured_output(RouterDecision)
        result: RouterDecision = structured_llm.invoke([HumanMessage(content=ROUTER_PROMPT.format(query=query))])
        logger.info(f"Router decision: intent={result.intent}, reason={result.reason}")
        return {"query_intent": result.intent}
    except Exception as e:
        logger.error(f"Router failed, defaulting to retrieval: {e}")
        return {"query_intent": "retrieval"}
