import logging

from langchain_core.messages import HumanMessage

from src.config.prompts import GRADER_PROMPT
from src.config.settings import settings
from src.models.schemas import GradeResult
from src.models.state import RAGState
from src.services.llm_factory import LLMFactory

logger = logging.getLogger(__name__)


def grader_node(state: RAGState) -> dict:
    """Grade each retrieved chunk for relevance. Filter to relevant-only."""
    query = state.get("sanitized_query", "")
    chunks = state.get("retrieved_chunks", [])

    if not chunks:
        return {
            "relevant_chunks": [],
            "grading_results": [],
        }

    llm = LLMFactory.get_grader_llm()
    structured_llm = llm.with_structured_output(GradeResult)

    grading_results = []
    relevant_chunks = []

    for i, chunk in enumerate(chunks):
        try:
            prompt = GRADER_PROMPT.format(query=query, chunk=chunk["content"])
            result: GradeResult = structured_llm.invoke([HumanMessage(content=prompt)])
            grading_results.append({"chunk_id": i, "relevant": result.relevant, "reason": result.reason})
            if result.relevant:
                relevant_chunks.append(chunk)
        except Exception as e:
            logger.warning(f"Grading failed for chunk {i}, marking as irrelevant: {e}")
            grading_results.append({"chunk_id": i, "relevant": False, "reason": f"Grading error: {e}"})

    logger.info(f"Grading: {len(relevant_chunks)}/{len(chunks)} chunks relevant (min={settings.GRADING_MIN_RELEVANT_CHUNKS})")

    return {
        "relevant_chunks": relevant_chunks,
        "grading_results": grading_results,
    }
