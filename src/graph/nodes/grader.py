import logging
from concurrent.futures import ThreadPoolExecutor

from langchain_core.messages import HumanMessage

from src.config.prompts import GRADER_PROMPT
from src.config.settings import settings
from src.models.schemas import GradeResult
from src.models.state import RAGState
from src.services.candidate_validator import validate_candidates
from src.services.llm_factory import LLMFactory
from src.services.llm_retry import retry_llm_call
from src.services.ltr_gate_service import build_features, dump_feature_log, score_candidates

logger = logging.getLogger(__name__)

# Parallelism for the per-chunk LLM grading stage. Each chunk's grader call is
# an independent OpenAI request, so an 8-wide ThreadPoolExecutor lets all 8
# (or however many survived stages 1+2) overlap. OpenAI tier-1 in-flight cap
# comfortably absorbs this for our eval workload (one query at a time, 8
# in-flight grader calls = well under tier limits).
_GRADER_PARALLELISM = 8


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
    target_fiscal_year = state.get("target_fiscal_year")

    if not chunks:
        return {
            "relevant_chunks": [],
            "grading_results": [],
            "grader_fallback_used": False,
        }

    candidate_diagnostics: list[dict] = []
    if settings.ENABLE_DETERMINISTIC_VALIDATOR:
        chunks, validator_diag = validate_candidates(
            query=query,
            candidates=chunks,
            target_company=target_company,
            target_fiscal_year=target_fiscal_year,
            min_keep=settings.VALIDATOR_MIN_KEEP,
        )
        candidate_diagnostics.extend(validator_diag)

    ltr_scores = None
    if settings.ENABLE_LTR_GATE:
        feats = build_features(query=query, candidates=chunks, target_company=target_company, target_fiscal_year=target_fiscal_year)
        ltr_scores = score_candidates(feats, settings.LTR_GATE_MODEL_PATH)
        dump_feature_log("data/diagnostics/ltr_features.jsonl", query, feats, ltr_scores)
        if ltr_scores:
            candidate_diagnostics.extend(
                {"candidate_id": i, "ltr_score": s} for i, s in enumerate(ltr_scores)
            )

    llm = LLMFactory.get_grader_llm()
    structured_llm = llm.with_structured_output(GradeResult)

    # Sprint 8e: per-(query, chunk) grader-verdict cache. Grader is
    # ~8 LLM calls per question and the same chunk gets re-graded across
    # retrieval retries on the same query — high-hit-rate, low-risk cache.
    # Verdict is deterministic at temperature=0 so caching is correctness-safe.
    from src.services.result_cache import _KEY_PREFIX, _client, _hash_key

    grader_cache_prefix = _KEY_PREFIX["grader"]
    grader_model = settings.GRADER_MODEL

    # Pre-pass (sequential, no LLM): apply stages 1, 2, and the cache lookup
    # to partition chunks into auto-decided (entity-reject / LTR-keep /
    # LTR-drop / cache-hit) vs needs-LLM-grading. Index each chunk's outcome
    # into a dict so we can rejoin in original order after the parallel LLM
    # stage completes.
    pending_indices: list[int] = []
    decisions: dict[int, dict] = {}  # chunk_id -> {relevant, reason}
    rejected_by_entity = 0
    cache_hits = 0

    for i, chunk in enumerate(chunks):
        # Stage 1: cheap metadata-based entity check
        if not _entity_match(chunk, target_company):
            rejected_by_entity += 1
            chunk_co = (chunk.get("metadata") or {}).get("company", "?")
            decisions[i] = {
                "relevant": False,
                "reason": f"Entity mismatch: chunk is from '{chunk_co}', query targets '{target_company}'",
            }
            continue

        # Stage 2: LTR high-confidence keep/drop (optional)
        if ltr_scores is not None and i < len(ltr_scores):
            s = ltr_scores[i]
            if s <= settings.LTR_GATE_LOW_CONFIDENCE:
                decisions[i] = {"relevant": False, "reason": f"LTR gate low-confidence drop ({s:.3f})"}
                continue
            if s >= settings.LTR_GATE_HIGH_CONFIDENCE:
                decisions[i] = {"relevant": True, "reason": f"LTR gate high-confidence keep ({s:.3f})"}
                continue

        # Stage 2.5: result-cache lookup. Skip the LLM call when we've
        # already evaluated this exact (model, query, chunk) tuple.
        chunk_text = chunk.get("content", "")
        cache_key = grader_cache_prefix + _hash_key(grader_model, query, chunk_text)
        cached = _client.get(cache_key)
        if isinstance(cached, dict) and "relevant" in cached:
            decisions[i] = {
                "relevant": bool(cached["relevant"]),
                "reason": cached.get("reason", "cache hit"),
            }
            cache_hits += 1
            continue

        # Stage 3 candidate — needs an LLM call
        pending_indices.append(i)

    # Stage 3 (parallel): LLM topic-relevance grading on the survivors.
    # Each call wrapped in retry_llm_call so transient OpenAI hiccups
    # (connection resets, rate-limit bursts, momentary 5xx) don't silently
    # turn into false-irrelevant verdicts — which previously corrupted ~1
    # FinanceBench question per run as a refusal in the cache.
    def _grade_one(idx: int) -> tuple[int, dict]:
        chunk = chunks[idx]
        chunk_text = chunk["content"]
        prompt = GRADER_PROMPT.format(query=query, chunk=chunk_text)

        def _invoke() -> GradeResult:
            return structured_llm.invoke([HumanMessage(content=prompt)])

        try:
            result: GradeResult = retry_llm_call(_invoke, label=f"grader chunk {idx}")
            decision = {"relevant": result.relevant, "reason": result.reason}
            # Sprint 8e: cache the verdict so the next retrieval-retry round
            # against this exact (query, chunk) pair skips the LLM call.
            cache_key = grader_cache_prefix + _hash_key(grader_model, query, chunk_text)
            _client.set(cache_key, decision)
            return idx, decision
        except Exception as e:
            logger.warning(f"Grading failed for chunk {idx} after retries, marking as irrelevant: {e}")
            # Don't cache errors — a transient failure should not poison
            # the verdict for future retries.
            return idx, {"relevant": False, "reason": f"Grading error: {type(e).__name__}: {str(e)[:200]}"}

    if pending_indices:
        # Cap parallelism at the smaller of (chunks needing LLM grading, configured limit)
        # so we never spawn more workers than necessary.
        max_workers = min(_GRADER_PARALLELISM, len(pending_indices))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for idx, decision in pool.map(_grade_one, pending_indices):
                decisions[idx] = decision

    # Reassemble in original chunk order (preserves reranker ordering downstream).
    grading_results = []
    relevant_chunks = []
    for i, chunk in enumerate(chunks):
        d = decisions.get(i, {"relevant": False, "reason": "missing decision (defensive)"})
        grading_results.append({"chunk_id": i, "relevant": d["relevant"], "reason": d["reason"]})
        if d["relevant"]:
            relevant_chunks.append(chunk)

    # Sprint 7.7 Day 7: empty-context fallback at the grader level.
    # When the grader rejects ALL chunks (LLM strictness, LTR overconfidence),
    # downstream gets [] and refuses. Hypothesis: pass through top-K reranker
    # chunks instead.
    #
    # EXPERIMENTAL — Day 7 dev-set test on FinanceBench (commit will follow):
    #   - 0/14 baseline empty-context cases rescued
    #   - 1 regression on a previously-passing question (added noise hurt synthesis)
    #   - Conclusion: rejected chunks really aren't relevant; forcing them
    #     through doesn't help. Same lesson as Sprint 7a's hybrid+rerank.
    #
    # Code retained behind ENABLE_GRADER_EMPTY_CONTEXT_FALLBACK feature flag
    # (default False) so future deployments with more lenient downstream
    # generators can opt-in without regressing FinanceBench.
    grader_fallback_used = False
    if (
        settings.ENABLE_GRADER_EMPTY_CONTEXT_FALLBACK
        and not relevant_chunks
        and chunks
    ):
        # Skip chunks the entity-match stage rejected — those are RBAC-adjacent
        # cross-company chunks we never want to leak through the pipeline.
        salvageable = [
            (i, chunk) for i, chunk in enumerate(chunks)
            if _entity_match(chunk, target_company)
        ]
        if salvageable:
            fallback_n = min(settings.GRADING_MIN_RELEVANT_CHUNKS, len(salvageable))
            relevant_chunks = [chunk for _, chunk in salvageable[:fallback_n]]
            for i, _ in salvageable[:fallback_n]:
                # Update grading_results so diagnostics see this was a fallback
                grading_results[i] = {
                    "chunk_id": i,
                    "relevant": True,
                    "reason": "GRADER FALLBACK: all chunks failed grading; passing through top reranked (best-effort)",
                }
            grader_fallback_used = True
            logger.warning(
                f"Grader fallback: 0/{len(chunks)} relevant after grading; passing through "
                f"top {fallback_n} reranked entity-matched chunks (best-effort)"
            )

    entity_msg = f", {rejected_by_entity} rejected by entity mismatch" if rejected_by_entity else ""
    cache_msg = f", {cache_hits} cache hits" if cache_hits else ""
    fallback_msg = " [GRADER FALLBACK]" if grader_fallback_used else ""
    logger.info(
        f"Grading: {len(relevant_chunks)}/{len(chunks)} chunks relevant "
        f"(min={settings.GRADING_MIN_RELEVANT_CHUNKS}{entity_msg}{cache_msg}, "
        f"{len(pending_indices)} sent to LLM in parallel){fallback_msg}"
    )

    return {
        "relevant_chunks": relevant_chunks,
        "grading_results": grading_results,
        "candidate_diagnostics": candidate_diagnostics,
        "grader_fallback_used": grader_fallback_used,
    }
