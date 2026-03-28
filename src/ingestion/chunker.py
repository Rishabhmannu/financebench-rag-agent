"""Chunk documents using Docling HybridChunker or fallback text splitter."""

import logging

logger = logging.getLogger(__name__)

# Target chunk size for financial documents
MAX_CHUNK_TOKENS = 512


def chunk_document(document: dict, metadata: dict) -> list[dict]:
    """Chunk a document into pieces with metadata attached to each chunk."""
    text = document.get("text", "")
    if not text.strip():
        return []

    # Use simple character-based splitting for now
    # TODO (Sprint 1): Replace with Docling HybridChunker once ingestion pipeline is proven
    chunks = _simple_chunk(text, chunk_size=2000, overlap=200)

    result = []
    for i, chunk_text in enumerate(chunks):
        result.append({
            "content": chunk_text,
            "metadata": {
                **metadata,
                "chunk_index": i,
            },
        })

    return result


def _simple_chunk(text: str, chunk_size: int = 2000, overlap: int = 200) -> list[str]:
    """Split text by paragraphs, merging until chunk_size is reached."""
    paragraphs = text.split("\n\n")
    chunks = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        if len(current) + len(para) + 2 > chunk_size and current:
            chunks.append(current.strip())
            # Keep overlap
            current = current[-overlap:] + "\n\n" + para if overlap else para
        else:
            current = current + "\n\n" + para if current else para

    if current.strip():
        chunks.append(current.strip())

    return chunks
