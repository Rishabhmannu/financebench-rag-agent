import logging

from langchain_core.messages import HumanMessage

from src.config.prompts import GRADER_PROMPT
from src.config.settings import settings
from src.models.schemas import GradeResult
from src.models.state import RAGState
from src.services.llm_factory import LLMFactory

logger = logging.getLogger(__name__)


def _entity_match(chunk: dict, target_company: str | None) -> bool:
    """Deterministic entity check using chunk metadata.

    Returns True if the chunk can participate in grading:
      - target_company is None → all chunks pass (comparative/generic query)
      - chunk.company matches target_company → pass
      - otherwise → reject without LLM call

    Retrieval already filters by company in Qdrant, so this is defense-in-depth
    for the rare case a chunk leaks through (e.g. a cross-company chunk with
    the target company's name inside its text).
    """
    if target_company is None:
        return True
    chunk_company = (chunk.get("metadata") or {}).get("company")
    return chunk_company == target_company


def grader_node(state: RAGState) -> dict:
    """Grade each reranked chunk for relevance. Filter to relevant-only.

    Two-stage filtering:
      1. Deterministic entity check — reject chunks whose `company` metadata
         doesn't match `target_company`. Zero LLM cost. (Sprint 7a.v2 addition.)
      2. LLM topic-relevance grading on the survivors.

    Reads from `reranked_chunks` (cross-encoder top-K from the hybrid candidate
    pool) so we only spend LLM calls on chunks that already passed the cheaper
    reranker filter. Falls back to `retrieved_chunks` if the reranker produced
    nothing (defensive).
    """
    query = state.get("sanitized_query", "")
    chunks = state.get("reranked_chunks") or state.get("retrieved_chunks", [])
    target_company = state.get("target_company")

    if not chunks:
        return {
            "relevant_chunks": [],
            "grading_results": [],
        }

    llm = LLMFactory.get_grader_llm()
    structured_llm = llm.with_structured_output(GradeResult)

    grading_results = []
    relevant_chunks = []
    rejected_by_entity = 0

    for i, chunk in enumerate(chunks):
        # Stage 1: cheap metadata-based entity check
        if not _entity_match(chunk, target_company):
            rejected_by_entity += 1
            chunk_co = (chunk.get("metadata") or {}).get("company", "?")
            grading_results.append({
                "chunk_id": i,
                "relevant": False,
                "reason": f"Entity mismatch: chunk is from '{chunk_co}', query targets '{target_company}'",
            })
            continue

        # Stage 2: LLM topic-relevance grading
        try:
            prompt = GRADER_PROMPT.format(query=query, chunk=chunk["content"])
            result: GradeResult = structured_llm.invoke([HumanMessage(content=prompt)])
            grading_results.append({"chunk_id": i, "relevant": result.relevant, "reason": result.reason})
            if result.relevant:
                relevant_chunks.append(chunk)
        except Exception as e:
            logger.warning(f"Grading failed for chunk {i}, marking as irrelevant: {e}")
            grading_results.append({"chunk_id": i, "relevant": False, "reason": f"Grading error: {e}"})

    entity_msg = f", {rejected_by_entity} rejected by entity mismatch" if rejected_by_entity else ""
    logger.info(
        f"Grading: {len(relevant_chunks)}/{len(chunks)} chunks relevant "
        f"(min={settings.GRADING_MIN_RELEVANT_CHUNKS}{entity_msg})"
    )

    return {
        "relevant_chunks": relevant_chunks,
        "grading_results": grading_results,
    }
