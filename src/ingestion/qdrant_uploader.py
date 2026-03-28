"""Upload chunked documents with embeddings to Qdrant."""

import logging
import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

from src.config.settings import settings
from src.services.embeddings import embed_texts

logger = logging.getLogger(__name__)

BATCH_SIZE = 50


def upload_chunks(client: QdrantClient, chunks: list[dict]) -> None:
    """Embed and upload chunks to Qdrant in batches."""
    for i in range(0, len(chunks), BATCH_SIZE):
        batch = chunks[i : i + BATCH_SIZE]
        texts = [c["content"] for c in batch]

        vectors = embed_texts(texts)

        points = [
            PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={"content": chunk["content"], **chunk["metadata"]},
            )
            for chunk, vector in zip(batch, vectors)
        ]

        client.upsert(collection_name=settings.QDRANT_COLLECTION, points=points)
        logger.debug(f"Uploaded batch of {len(points)} points to Qdrant")
