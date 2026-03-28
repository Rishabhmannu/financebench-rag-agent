import logging

from langchain_core.messages import HumanMessage

from src.config.prompts import QUERY_REWRITER_PROMPT
from src.models.state import RAGState
from src.services.llm_factory import LLMFactory

logger = logging.getLogger(__name__)


def query_rewriter_node(state: RAGState) -> dict:
    """Rewrite the query based on grading feedback to improve retrieval."""
    query = state.get("sanitized_query", "")
    grading_results = state.get("grading_results", [])
    retry_count = state.get("retrieval_retry_count", 0)

    # Build feedback from irrelevant chunks
    feedback_lines = [r["reason"] for r in grading_results if not r.get("relevant")]
    feedback = "\n".join(feedback_lines[:3]) if feedback_lines else "Chunks were not relevant to the question."

    try:
        llm = LLMFactory.get_router_llm()
        prompt = QUERY_REWRITER_PROMPT.format(query=query, feedback=feedback)
        result = llm.invoke([HumanMessage(content=prompt)])
        rewritten = result.content.strip()
        logger.info(f"Query rewritten (attempt {retry_count + 1}): '{query[:50]}' -> '{rewritten[:50]}'")
        return {
            "retrieval_query": rewritten,
            "retrieval_retry_count": retry_count + 1,
        }
    except Exception as e:
        logger.error(f"Query rewriting failed: {e}")
        return {
            "retrieval_query": query,
            "retrieval_retry_count": retry_count + 1,
        }
