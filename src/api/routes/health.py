from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health_check():
    return {"status": "ok"}


@router.get("/warm")
async def warm():
    """Force lazy-loaded models + provider HTTP clients to load. Called by the
    CLI's REPL at boot so the first query isn't slower than subsequent ones.
    Cheap if already warm.

    Bug D (audit): testers occasionally hit a cold first-query path that
    returned an empty no-info refusal even after /warm. Root cause was that
    /warm only covered the BGE reranker + sparse BM25 embedder, not the dense
    embedding provider HTTP client (voyage/openai TLS handshake) or the grader
    LLM connection pool. A cold grader call with strict thresholds rejects all
    chunks → routes to no_info_node. Now /warm also forces a tiny dense embed
    + instantiates the grader/generator LLMs (clients are cached after the
    first instantiation per LLMFactory's classmethod pattern).
    """
    loaded: dict[str, str] = {}
    try:
        from src.services.reranker_service import get_reranker
        rk = get_reranker()
        loaded["reranker"] = type(rk).__name__
    except Exception as e:
        loaded["reranker"] = f"error: {type(e).__name__}: {e}"
    try:
        from src.services.vector_store import get_sparse_embedder
        emb = get_sparse_embedder()
        loaded["sparse_embedder"] = type(emb).__name__
    except Exception as e:
        loaded["sparse_embedder"] = f"error: {type(e).__name__}: {e}"
    try:
        from src.services.embeddings import embed_text
        v = embed_text("warmup", input_type="query")
        loaded["dense_embedder"] = f"dim={len(v)}"
    except Exception as e:
        loaded["dense_embedder"] = f"error: {type(e).__name__}: {e}"
    try:
        from src.services.llm_factory import LLMFactory
        # Instantiating these populates the provider connection pool; the
        # underlying LangChain clients are reused across requests, so the
        # next real call pays no TLS-handshake cost.
        LLMFactory.get_grader_llm()
        LLMFactory.get_generator_llm()
        LLMFactory.get_router_llm()
        loaded["llms"] = "grader+generator+router instantiated"
    except Exception as e:
        loaded["llms"] = f"error: {type(e).__name__}: {e}"
    return {"status": "warm", "loaded": loaded}
