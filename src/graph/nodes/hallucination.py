import logging

from langchain_core.messages import HumanMessage

from src.config.prompts import HALLUCINATION_CHECK_PROMPT
from src.models.schemas import HallucinationCheck
from src.models.state import RAGState
from src.services.llm_factory import LLMFactory

logger = logging.getLogger(__name__)


def hallucination_checker_node(state: RAGState) -> dict:
    """Check if the generated answer is grounded in the retrieved sources."""
    answer = state.get("generated_answer", "")
    chunks = state.get("relevant_chunks", [])

    if not answer or not chunks:
        return {"hallucination_status": "grounded", "hallucination_score": 1.0}

    sources = "\n\n---\n\n".join(
        f"[{chunk.get('metadata', {}).get('source_file', 'Unknown')}]\n{chunk['content']}" for chunk in chunks
    )

    prompt = HALLUCINATION_CHECK_PROMPT.format(sources=sources, answer=answer)

    retry_count = state.get("generation_retry_count", 0)

    try:
        llm = LLMFactory.get_hallucination_llm()
        structured_llm = llm.with_structured_output(HallucinationCheck)
        result: HallucinationCheck = structured_llm.invoke([HumanMessage(content=prompt)])

        logger.info(f"Hallucination check: grounded={result.grounded}, score={result.score:.2f}")

        is_grounded = result.grounded
        return {
            "hallucination_status": "grounded" if is_grounded else "hallucinated",
            "hallucination_score": result.score,
            "generation_retry_count": retry_count if is_grounded else retry_count + 1,
        }
    except Exception as e:
        logger.error(f"Hallucination check failed, assuming grounded: {e}")
        return {
            "hallucination_status": "grounded",
            "hallucination_score": 0.5,
            "generation_retry_count": retry_count,
        }
