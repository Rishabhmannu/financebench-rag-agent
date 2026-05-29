from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health_check():
    return {"status": "ok"}


@router.get("/warm")
async def warm():
    """Deep pre-warm: load AND EXERCISE every cold-start-prone component.

    0.1.0 → 0.1.1: extended warm to also load dense embedding client + LLM
    factories. 0.1.1 → 0.1.2: actually run a dummy inference through each
    component. The 0.1.1 warm INSTANTIATED LangChain LLM client objects but
    didn't make HTTP calls. The first real query still paid the TLS-handshake
    + connection-pool init cost. On the M1 test, this manifested as ~3 min
    of invisible time in guardrails (LLM Guard transformer load) + slow first
    LLM RPCs.

    Cost: ~$0.005 per warm (4-5 tiny LLM calls + 1 OpenAI embed). One-time
    per uvicorn process — eats the cold-start tax before the user sees it.
    Total wall time: 10-30s on M1, ~5-10s on M4.
    """
    import time
    timings_ms: dict[str, int] = {}
    loaded: dict[str, str] = {}

    def _timed(name: str, fn):
        """Run fn(), record elapsed ms or capture error string into loaded."""
        t0 = time.monotonic()
        try:
            result = fn()
            timings_ms[name] = int((time.monotonic() - t0) * 1000)
            return result
        except Exception as e:
            timings_ms[name] = int((time.monotonic() - t0) * 1000)
            loaded[name] = f"error: {type(e).__name__}: {str(e)[:120]}"
            return None

    # 1. BGE reranker — actual dummy forward pass (forces weight load + JIT).
    def _warm_reranker():
        from src.services.reranker_service import get_reranker
        rk = get_reranker()
        # Score a dummy (query, chunk) pair to actually run the cross-encoder.
        rk.predict([("warmup query", "warmup passage")])
        return type(rk).__name__
    res = _timed("reranker", _warm_reranker)
    if res is not None:
        loaded["reranker"] = res

    # 2. Sparse BM25 embedder — load + embed a token (FastEmbed lazy loads).
    def _warm_sparse():
        from src.services.vector_store import compute_sparse_vectors
        compute_sparse_vectors(["warmup"])
        return "SparseTextEmbedding"
    res = _timed("sparse_embedder", _warm_sparse)
    if res is not None:
        loaded["sparse_embedder"] = res

    # 3. Dense embedder — one HTTP roundtrip to OpenAI/Voyage so TLS pool is up.
    def _warm_dense():
        from src.services.embeddings import embed_text
        v = embed_text("warmup", input_type="query")
        return f"dim={len(v)}"
    res = _timed("dense_embedder", _warm_dense)
    if res is not None:
        loaded["dense_embedder"] = res

    # 4. Guardrails LLM Guard — local transformer; biggest cold-start source.
    # M1 evidence: 3-min uninstrumented time before entity_extractor on first
    # query. Almost certainly this model loading. Force it here instead.
    def _warm_guardrails():
        from src.services.guardrails import check_injection_llm_guard
        check_injection_llm_guard("warmup query")
        return "LLMGuard"
    res = _timed("guardrails", _warm_guardrails)
    if res is not None:
        loaded["guardrails"] = res

    # 5. Entity-extractor LLM — one tiny invoke to warm the HTTP pool.
    def _warm_entity():
        from langchain_core.messages import HumanMessage
        from src.services.llm_factory import LLMFactory
        llm = LLMFactory.get_router_llm()  # entity_extractor uses same provider
        llm.invoke([HumanMessage(content="ok")])
        return type(llm).__name__
    res = _timed("entity_llm", _warm_entity)
    if res is not None:
        loaded["entity_llm"] = res

    # 6. Grader LLM (might be Groq/OpenAI depending on USE_GROQ_FAST_PATH).
    def _warm_grader():
        from langchain_core.messages import HumanMessage
        from src.services.llm_factory import LLMFactory
        llm = LLMFactory.get_grader_llm()
        llm.invoke([HumanMessage(content="ok")])
        return type(llm).__name__
    res = _timed("grader_llm", _warm_grader)
    if res is not None:
        loaded["grader_llm"] = res

    # 7. Generator LLM (Claude Sonnet). Tiny prompt — pennies of cost.
    def _warm_generator():
        from langchain_core.messages import HumanMessage
        from src.services.llm_factory import LLMFactory
        llm = LLMFactory.get_generator_llm()
        llm.invoke([HumanMessage(content="ok")])
        return type(llm).__name__
    res = _timed("generator_llm", _warm_generator)
    if res is not None:
        loaded["generator_llm"] = res

    total_ms = sum(timings_ms.values())
    return {
        "status": "warm",
        "loaded": loaded,
        "timings_ms": timings_ms,
        "total_ms": total_ms,
    }
