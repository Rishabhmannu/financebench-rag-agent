import logging

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, FieldCondition, Filter, MatchAny, PayloadSchemaType, VectorParams

from src.config.settings import settings

logger = logging.getLogger(__name__)


def get_qdrant_client() -> QdrantClient:
    return QdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)


def ensure_collection(client: QdrantClient) -> None:
    """Create the collection and payload indexes if they don't exist."""
    collections = [c.name for c in client.get_collections().collections]
    if settings.QDRANT_COLLECTION not in collections:
        client.create_collection(
            collection_name=settings.QDRANT_COLLECTION,
            vectors_config=VectorParams(size=settings.EMBEDDING_DIMENSIONS, distance=Distance.COSINE),
        )
        # Payload indexes for fast RBAC filtering
        for field in ("doc_type", "confidentiality", "company"):
            client.create_payload_index(
                collection_name=settings.QDRANT_COLLECTION,
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
            )
        logger.info(f"Created collection '{settings.QDRANT_COLLECTION}' with payload indexes")


def build_rbac_filter(allowed_doc_types: list[str], allowed_confidentiality: list[str]) -> Filter | None:
    """Build a Qdrant filter from RBAC permissions."""
    conditions = []

    if "*" not in allowed_doc_types:
        conditions.append(FieldCondition(key="doc_type", match=MatchAny(any=allowed_doc_types)))

    if "*" not in allowed_confidentiality:
        conditions.append(FieldCondition(key="confidentiality", match=MatchAny(any=allowed_confidentiality)))

    return Filter(must=conditions) if conditions else None


def search(
    client: QdrantClient,
    query_vector: list[float],
    rbac_filter: Filter | None = None,
    top_k: int = 8,
) -> list[dict]:
    """Search Qdrant with optional RBAC filter. Returns list of chunk dicts."""
    results = client.query_points(
        collection_name=settings.QDRANT_COLLECTION,
        query=query_vector,
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
