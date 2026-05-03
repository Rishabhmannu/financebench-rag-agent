import logging

from src.config.rbac_config import get_permissions
from src.config.settings import settings
from src.models.state import RAGState
from src.services.embeddings import embed_text
from src.services.vector_store import build_retrieval_filter, get_qdrant_client, hybrid_search

logger = logging.getLogger(__name__)


def retrieval_node(state: RAGState) -> dict:
    """Hybrid retrieval (dense OpenAI embeddings + BM25 sparse, fused via RRF).

    Returns a wide candidate pool (top_k=50 by default) — the reranker node
    narrows it to the final set used by grader + generator.
    """
    query = state.get("retrieval_query") or state.get("sanitized_query", "")
    allowed_doc_types = state.get("allowed_doc_types", ["10k"])
    user_role = state.get("user_role", "analyst")
    target_company = state.get("target_company")
    target_fiscal_year = state.get("target_fiscal_year")

    permissions = get_permissions(user_role)
    allowed_confidentiality = permissions["allowed_confidentiality"]

    try:
        query_vector = embed_text(query)
        retrieval_filter = build_retrieval_filter(
            allowed_doc_types=allowed_doc_types,
            allowed_confidentiality=allowed_confidentiality,
            target_company=target_company,
            target_fiscal_year=target_fiscal_year,
        )
        client = get_qdrant_client()

        chunks = hybrid_search(
            client=client,
            query_text=query,
            query_dense_vector=query_vector,
            rbac_filter=retrieval_filter,
            top_k=settings.RETRIEVAL_TOP_K,
        )
        initial_count = len(chunks)
        retrieval_fallback_used = False

        # Progressive filter relaxation: if strict entity+year filtering yields
        # too few candidates, drop year first, then company. Sprint 7.7 Day 7:
        # set `retrieval_fallback_used` when ANY relaxation kicks in so
        # diagnostics can distinguish "filter-relaxed" answers from
        # confidently-filtered ones.
        if len(chunks) < settings.GRADING_MIN_RELEVANT_CHUNKS:
            if target_company and target_fiscal_year:
                relaxed_filter = build_retrieval_filter(
                    allowed_doc_types=allowed_doc_types,
                    allowed_confidentiality=allowed_confidentiality,
                    target_company=target_company,
                    target_fiscal_year=None,
                )
                relaxed = hybrid_search(
                    client=client,
                    query_text=query,
                    query_dense_vector=query_vector,
                    rbac_filter=relaxed_filter,
                    top_k=settings.RETRIEVAL_TOP_K,
                )
                if len(relaxed) > len(chunks):
                    chunks = relaxed
                    retrieval_fallback_used = True
            if len(chunks) < settings.GRADING_MIN_RELEVANT_CHUNKS and target_company:
                relaxed_filter = build_retrieval_filter(
                    allowed_doc_types=allowed_doc_types,
                    allowed_confidentiality=allowed_confidentiality,
                    target_company=None,
                    target_fiscal_year=None,
                )
                relaxed = hybrid_search(
                    client=client,
                    query_text=query,
                    query_dense_vector=query_vector,
                    rbac_filter=relaxed_filter,
                    top_k=settings.RETRIEVAL_TOP_K,
                )
                if len(relaxed) > len(chunks):
                    chunks = relaxed
                    retrieval_fallback_used = True

        filter_info = f"company={target_company},year={target_fiscal_year}" if target_company else "no-company-filter"
        fallback_info = f" [FALLBACK from {initial_count}]" if retrieval_fallback_used else ""
        logger.info(f"Retrieved {len(chunks)} hybrid candidates ({filter_info}){fallback_info} for: {query[:60]}...")
        return {
            "retrieved_chunks": chunks,
            "retrieval_fallback_used": retrieval_fallback_used,
        }

    except Exception as e:
        logger.error(f"Retrieval failed: {e}", exc_info=True)
        return {"retrieved_chunks": [], "retrieval_fallback_used": False}
