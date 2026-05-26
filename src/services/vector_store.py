"""Qdrant client + hybrid retrieval.

Collections use **named vectors**: a dense vector (OpenAI embeddings) for
semantic similarity plus a sparse BM25 vector for keyword matching. Searches
fuse both via Qdrant's Reciprocal Rank Fusion (RRF) so the final top-K reflects
both semantic meaning and exact-term overlap.

Hybrid retrieval fixes the vocabulary gap observed in the Sprint-6 baseline:
eval questions use casual phrasing ("Apple's revenue") while real 10-Ks use
formal terminology ("Total net sales"). Dense embeddings miss that kind of
surface-form match; BM25 catches it.
"""

import logging
from datetime import datetime, timezone
from functools import lru_cache

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    Fusion,
    FusionQuery,
    MatchAny,
    PayloadSchemaType,
    PointStruct,
    Prefetch,
    SparseIndexParams,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from src.config.settings import settings

logger = logging.getLogger(__name__)

DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "sparse"
BM25_MODEL_NAME = "Qdrant/bm25"

# Deployment plan Section 18.3.4 — embedding-model fingerprint.
# A reserved-UUID sentinel point holds the embedding config used at collection
# create time, so the boot banner can detect silent embedding-model swaps that
# would otherwise return garbage (incompatible vector dim or different latent
# space). UUID is deterministic + out-of-band from the random UUIDs ingestion
# uses (src/ingestion/qdrant_uploader.py:38).
SENTINEL_POINT_ID = "00000000-0000-0000-0000-000000000001"
META_PAYLOAD_KEY = "_meta"


def get_qdrant_client() -> QdrantClient:
    return QdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)


@lru_cache(maxsize=1)
def get_sparse_embedder():
    """Lazily load the BM25 sparse embedder (fastembed). Cached — loading is not cheap."""
    from fastembed import SparseTextEmbedding

    logger.info(f"Loading sparse embedder: {BM25_MODEL_NAME}")
    return SparseTextEmbedding(model_name=BM25_MODEL_NAME)


def compute_sparse_vectors(texts: list[str]) -> list[SparseVector]:
    """Return a list of Qdrant SparseVector objects, one per input text."""
    embedder = get_sparse_embedder()
    return [
        SparseVector(indices=list(e.indices), values=list(e.values))
        for e in embedder.embed(texts)
    ]


def ensure_collection(client: QdrantClient, collection_name: str | None = None) -> None:
    """Create the collection and payload indexes if they don't exist.

    Schema: named dense vector + named sparse vector. If an older single-vector
    collection exists, callers should drop it first — this function will not
    migrate an existing collection's schema.

    `collection_name` override lets callers target non-default collections
    (e.g. FinanceBench corpus isolated from the production collection).
    """
    target = collection_name or settings.QDRANT_COLLECTION
    collections = [c.name for c in client.get_collections().collections]
    if target in collections:
        return

    client.create_collection(
        collection_name=target,
        vectors_config={
            DENSE_VECTOR_NAME: VectorParams(
                size=settings.EMBEDDING_DIMENSIONS, distance=Distance.COSINE
            ),
        },
        sparse_vectors_config={
            SPARSE_VECTOR_NAME: SparseVectorParams(index=SparseIndexParams(on_disk=False)),
        },
    )
    # Payload indexes for fast RBAC filtering
    for field in ("doc_type", "confidentiality", "company"):
        client.create_payload_index(
            collection_name=target,
            field_name=field,
            field_schema=PayloadSchemaType.KEYWORD,
        )
    # Optional year filter index. Backward compatible with existing collections.
    try:
        client.create_payload_index(
            collection_name=target,
            field_name="fiscal_year",
            field_schema=PayloadSchemaType.INTEGER,
        )
    except Exception:
        pass

    try:
        upsert_collection_fingerprint(
            client=client,
            collection_name=target,
            embedding_model=settings.EMBEDDING_MODEL,
            embedding_dim=settings.EMBEDDING_DIMENSIONS,
            embedding_provider=settings.EMBEDDING_PROVIDER,
        )
    except Exception as exc:
        logger.warning("Failed to write embedding-model fingerprint to '%s': %s", target, exc)

    logger.info(f"Created hybrid collection '{target}' with dense + sparse vectors")


def upsert_collection_fingerprint(
    client: QdrantClient,
    collection_name: str,
    embedding_model: str,
    embedding_dim: int,
    embedding_provider: str,
) -> None:
    """Write/refresh the sentinel point with the embedding config used at
    collection-create time. Zero dense vector + empty sparse vector so it
    ranks near-bottom in any retrieval (never enters top-K of meaningful
    queries). Boot banner reads this back to detect silent config drift.
    """
    client.upsert(
        collection_name=collection_name,
        points=[
            PointStruct(
                id=SENTINEL_POINT_ID,
                vector={
                    DENSE_VECTOR_NAME: [0.0] * embedding_dim,
                    SPARSE_VECTOR_NAME: SparseVector(indices=[], values=[]),
                },
                payload={
                    META_PAYLOAD_KEY: True,
                    "embedding_model": embedding_model,
                    "embedding_dim": embedding_dim,
                    "embedding_provider": embedding_provider,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        ],
    )


def get_collection_fingerprint(client: QdrantClient, collection_name: str) -> dict | None:
    """Read back the sentinel payload. Returns None if absent (collection
    pre-dates this feature — boot banner treats that as 'unknown', not error)."""
    try:
        result = client.retrieve(
            collection_name=collection_name,
            ids=[SENTINEL_POINT_ID],
            with_payload=True,
            with_vectors=False,
        )
        if not result:
            return None
        return dict(result[0].payload or {})
    except Exception:
        return None


def build_rbac_filter(allowed_doc_types: list[str], allowed_confidentiality: list[str]) -> Filter | None:
    """Build a Qdrant filter from RBAC permissions."""
    conditions = []

    if "*" not in allowed_doc_types:
        conditions.append(FieldCondition(key="doc_type", match=MatchAny(any=allowed_doc_types)))

    if "*" not in allowed_confidentiality:
        conditions.append(FieldCondition(key="confidentiality", match=MatchAny(any=allowed_confidentiality)))

    return Filter(must=conditions) if conditions else None


def build_retrieval_filter(
    allowed_doc_types: list[str],
    allowed_confidentiality: list[str],
    target_company: str | None = None,
    target_fiscal_year: int | None = None,
) -> Filter | None:
    """Build a combined retrieval filter: RBAC + entity (Sprint 7a.v2).

    When `target_company` is set (e.g. "apple"), adds a payload condition that
    physically excludes chunks from other companies — eliminates BM25 cross-
    entity contamination. When None (comparative or generic queries), only
    RBAC filtering applies.
    """
    conditions: list = []

    if "*" not in allowed_doc_types:
        conditions.append(FieldCondition(key="doc_type", match=MatchAny(any=allowed_doc_types)))

    if "*" not in allowed_confidentiality:
        conditions.append(FieldCondition(key="confidentiality", match=MatchAny(any=allowed_confidentiality)))

    if target_company:
        conditions.append(FieldCondition(key="company", match=MatchAny(any=[target_company])))
    if target_fiscal_year:
        conditions.append(FieldCondition(key="fiscal_year", match=MatchAny(any=[target_fiscal_year])))

    return Filter(must=conditions) if conditions else None


def hybrid_search(
    client: QdrantClient,
    query_text: str,
    query_dense_vector: list[float],
    rbac_filter: Filter | None = None,
    top_k: int = 50,
    fusion_candidates_per_arm: int = 50,
) -> list[dict]:
    """Hybrid retrieval: dense + sparse, fused via RRF.

    The caller passes the dense embedding (we already embed the query for the
    semantic stage). We compute the sparse vector here since it's a local call
    with no API cost.

    Returns `top_k` candidates; for Sprint-7a, retrieval returns ~50 candidates
    and a downstream reranker narrows to the final 5.
    """
    sparse = compute_sparse_vectors([query_text])[0]

    results = client.query_points(
        collection_name=settings.QDRANT_COLLECTION,
        prefetch=[
            Prefetch(
                query=query_dense_vector,
                using=DENSE_VECTOR_NAME,
                limit=fusion_candidates_per_arm,
                filter=rbac_filter,
            ),
            Prefetch(
                query=sparse,
                using=SPARSE_VECTOR_NAME,
                limit=fusion_candidates_per_arm,
                filter=rbac_filter,
            ),
        ],
        query=FusionQuery(fusion=Fusion.RRF),
        query_filter=rbac_filter,
        limit=top_k,
        with_payload=True,
    )
    return [
        {
            "content": point.payload.get("content", ""),
            "metadata": {k: v for k, v in point.payload.items() if k != "content"},
            "score": point.score,
        }
        for point in results.points
    ]


# Legacy dense-only search — kept for any caller that wants pure semantic search
# (e.g. debugging, benchmarking). The graph uses hybrid_search.
def search(
    client: QdrantClient,
    query_vector: list[float],
    rbac_filter: Filter | None = None,
    top_k: int = 8,
) -> list[dict]:
    """Pure dense semantic search. Prefer hybrid_search for production retrieval."""
    results = client.query_points(
        collection_name=settings.QDRANT_COLLECTION,
        query=query_vector,
        using=DENSE_VECTOR_NAME,
        query_filter=rbac_filter,
        limit=top_k,
        with_payload=True,
    )
    return [
        {
            "content": point.payload.get("content", ""),
            "metadata": {k: v for k, v in point.payload.items() if k != "content"},
            "score": point.score,
        }
        for point in results.points
    ]
