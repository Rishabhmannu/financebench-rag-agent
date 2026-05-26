from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health_check():
    return {"status": "ok"}


@router.get("/warm")
async def warm():
    """Force lazy-loaded models (BGE reranker, sparse embedder) to load. Called
    by the CLI's REPL at boot so the first query isn't slower than subsequent
    ones. Cheap if already warm.
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
    return {"status": "warm", "loaded": loaded}
