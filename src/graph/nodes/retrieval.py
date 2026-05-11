import logging

from src.config.rbac_config import get_permissions
from src.config.settings import settings
from src.models.state import RAGState
from src.services.embeddings import embed_text
from src.services.multi_hyde import generate_hypotheticals
from src.services.vector_store import build_retrieval_filter, get_qdrant_client, hybrid_search

logger = logging.getLogger(__name__)


# RRF constant — standard 60. Higher values flatten rank differences across
# paths; lower values amplify the top-ranked items. 60 is the literature
# default (Cormack et al. 2009) and matches what Qdrant uses internally.
_RRF_K = 60


def _chunk_id(chunk: dict) -> tuple:
    """Stable identity key for dedup across HyDE retrieval paths."""
    meta = chunk.get("metadata", {})
    return (
        meta.get("source_file", "?"),
        meta.get("chunk_index", meta.get("id", id(chunk))),
    )


def _rrf_fuse(paths: list[list[dict]], top_k: int) -> list[dict]:
    """Reciprocal-rank-fuse chunks across multiple retrieval paths.

    Each path is an already-ranked list (best first). A chunk's RRF score is
    `sum(1 / (k + rank_i))` across paths where it appears. Returns the top_k
    chunks by RRF score, with the highest-scoring instance per chunk_id used
    to carry payload + metadata.
    """
    by_id: dict[tuple, dict] = {}
    scores: dict[tuple, float] = {}
    for path in paths:
        for rank, chunk in enumerate(path):
            cid = _chunk_id(chunk)
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (_RRF_K + rank + 1)
            existing = by_id.get(cid)
            if existing is None or chunk.get("score", 0.0) > existing.get("score", 0.0):
                by_id[cid] = chunk
    ranked_ids = sorted(scores.keys(), key=lambda cid: scores[cid], reverse=True)
    return [by_id[cid] for cid in ranked_ids[:top_k]]


def retrieval_node(state: RAGState) -> dict:
    """Hybrid retrieval (dense + BM25 sparse, fused via RRF).

    Returns a wide candidate pool (top_k=50 by default) — the reranker node
    narrows it to the final set used by grader + generator.

    Sprint 7.10a — Multi-HyDE (`ENABLE_MULTI_HYDE`): also generates N
    hypothetical 10-K-style passages, embeds each, runs hybrid_search per
    path, then RRF-fuses results across (original + N hypothetical) paths.
    On Multi-HyDE generation failure, falls through to the single-query path.
    """
    query = state.get("retrieval_query") or state.get("sanitized_query", "")
    allowed_doc_types = state.get("allowed_doc_types", ["10k"])
    user_role = state.get("user_role", "analyst")
    target_company = state.get("target_company")
    target_fiscal_year = state.get("target_fiscal_year")

    permissions = get_permissions(user_role)
    allowed_confidentiality = permissions["allowed_confidentiality"]

    try:
        retrieval_filter = build_retrieval_filter(
            allowed_doc_types=allowed_doc_types,
            allowed_confidentiality=allowed_confidentiality,
            target_company=target_company,
            target_fiscal_year=target_fiscal_year,
        )
        client = get_qdrant_client()

        chunks = _multi_path_search(
            client=client,
            query=query,
            target_company=target_company,
            target_fiscal_year=target_fiscal_year,
            rbac_filter=retrieval_filter,
            top_k=settings.RETRIEVAL_TOP_K,
        )
        initial_count = len(chunks)
        retrieval_fallback_used = False

        # Progressive filter relaxation: if strict entity+year filtering yields
        # too few candidates, drop year first, then company.
        if len(chunks) < settings.GRADING_MIN_RELEVANT_CHUNKS:
            if target_company and target_fiscal_year:
                relaxed_filter = build_retrieval_filter(
                    allowed_doc_types=allowed_doc_types,
                    allowed_confidentiality=allowed_confidentiality,
                    target_company=target_company,
                    target_fiscal_year=None,
                )
                relaxed = _multi_path_search(
                    client=client,
                    query=query,
                    target_company=target_company,
                    target_fiscal_year=None,
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
                relaxed = _multi_path_search(
                    client=client,
                    query=query,
                    target_company=None,
                    target_fiscal_year=None,
                    rbac_filter=relaxed_filter,
                    top_k=settings.RETRIEVAL_TOP_K,
                )
                if len(relaxed) > len(chunks):
                    chunks = relaxed
                    retrieval_fallback_used = True

        filter_info = f"company={target_company},year={target_fiscal_year}" if target_company else "no-company-filter"
        fallback_info = f" [FALLBACK from {initial_count}]" if retrieval_fallback_used else ""
        hyde_info = " [multi-HyDE]" if settings.ENABLE_MULTI_HYDE else ""
        logger.info(f"Retrieved {len(chunks)} hybrid candidates ({filter_info}){fallback_info}{hyde_info} for: {query[:60]}...")
        return {
            "retrieved_chunks": chunks,
            "retrieval_fallback_used": retrieval_fallback_used,
        }

    except Exception as e:
        logger.error(f"Retrieval failed: {e}", exc_info=True)
        return {"retrieved_chunks": [], "retrieval_fallback_used": False}


def _multi_path_search(
    *,
    client,
    query: str,
    target_company: str | None,
    target_fiscal_year: int | None,
    rbac_filter,
    top_k: int,
) -> list[dict]:
    """Run hybrid_search for the original query and (when enabled) N
    Multi-HyDE hypotheticals, then RRF-fuse across paths.

    Falls back to single-path when Multi-HyDE is disabled or generation
    returned no hypotheticals.
    """
    base_results = hybrid_search(
        client=client,
        query_text=query,
        query_dense_vector=embed_text(query),
        rbac_filter=rbac_filter,
        top_k=top_k,
    )

    if not settings.ENABLE_MULTI_HYDE:
        return base_results

    hypotheticals = generate_hypotheticals(
        query=query,
        target_company=target_company,
        target_fiscal_year=target_fiscal_year,
        n=settings.MULTI_HYDE_N,
    )
    if not hypotheticals:
        return base_results

    paths = [base_results]
    for h in hypotheticals:
        try:
            hyde_results = hybrid_search(
                client=client,
                query_text=h,
                query_dense_vector=embed_text(h),
                rbac_filter=rbac_filter,
                top_k=top_k,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Multi-HyDE path search failed ({type(exc).__name__}): {exc}")
            continue
        paths.append(hyde_results)

    logger.info(f"Multi-HyDE: fused {len(paths)} retrieval paths ({len(hypotheticals)} hypotheticals)")
    return _rrf_fuse(paths, top_k=top_k)
