"""Upload chunked documents with dense + sparse embeddings to Qdrant."""

import logging
import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

from src.config.settings import settings
from src.services.embeddings import embed_texts
from src.services.vector_store import (
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    compute_sparse_vectors,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 50


def upload_chunks(client: QdrantClient, chunks: list[dict], collection_name: str | None = None) -> None:
    """Embed and upload chunks with both dense (OpenAI) and sparse (BM25) vectors.

    `collection_name` override targets a non-default Qdrant collection (e.g.
    FinanceBench corpus isolated from the main `financial_docs` collection).
    """
    target = collection_name or settings.QDRANT_COLLECTION
    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        texts = [c["content"] for c in batch]

        dense_vectors = embed_texts(texts)
        sparse_vectors = compute_sparse_vectors(texts)

        points = [
            PointStruct(
                id=str(uuid.uuid4()),
                vector={
                    DENSE_VECTOR_NAME: dense,
                    SPARSE_VECTOR_NAME: sparse,
                },
                payload={"content": chunk["content"], **chunk["metadata"]},
            )
            for chunk, dense, sparse in zip(batch, dense_vectors, sparse_vectors)
        ]

        client.upsert(collection_name=target, points=points)
        logger.debug(f"Uploaded batch of {len(points)} points (dense+sparse) to Qdrant '{target}'")
