import logging

from src.config.rbac_config import get_permissions
from src.config.settings import settings
from src.models.state import RAGState
from src.services.embeddings import embed_text
from src.services.vector_store import build_rbac_filter, get_qdrant_client, search

logger = logging.getLogger(__name__)


def retrieval_node(state: RAGState) -> dict:
    """Query Qdrant with RBAC-scoped filter and return top-k chunks."""
    query = state.get("retrieval_query") or state.get("sanitized_query", "")
    allowed_doc_types = state.get("allowed_doc_types", ["10k"])
    user_role = state.get("user_role", "analyst")

    permissions = get_permissions(user_role)
    allowed_confidentiality = permissions["allowed_confidentiality"]

    try:
        query_vector = embed_text(query)
        rbac_filter = build_rbac_filter(allowed_doc_types, allowed_confidentiality)
        client = get_qdrant_client()

        chunks = search(
            client=client,
            query_vector=query_vector,
            rbac_filter=rbac_filter,
            top_k=settings.RETRIEVAL_TOP_K,
        )

        logger.info(f"Retrieved {len(chunks)} chunks for query: {query[:80]}...")
        return {"retrieved_chunks": chunks}

    except Exception as e:
        logger.error(f"Retrieval failed: {e}")
        return {"retrieved_chunks": []}
