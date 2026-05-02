"""Main document ingestion orchestrator: PDF -> Docling -> Chunk -> Embed -> Qdrant.

Callers may pass a `collection_name` override to route chunks to a non-default
Qdrant collection (used by the FinanceBench adapter to keep its corpus isolated
from the main `financial_docs` collection).
"""

import logging
from pathlib import Path

from src.config.settings import settings
from src.ingestion.chunker import chunk_document
from src.ingestion.docling_loader import load_pdf
from src.ingestion.metadata_extractor import extract_metadata
from src.ingestion.qdrant_uploader import upload_chunks
from src.services.vector_store import ensure_collection, get_qdrant_client

logger = logging.getLogger(__name__)


def ingest_file(
    file_path: Path,
    doc_type: str | None = None,
    collection_name: str | None = None,
    metadata_override: dict | None = None,
) -> int:
    """Ingest a single PDF file. Returns number of chunks uploaded.

    Args:
        file_path: path to the PDF
        doc_type: override the auto-detected doc_type (useful for e.g. "10k_financebench")
        collection_name: override the default Qdrant collection (settings.QDRANT_COLLECTION)
        metadata_override: dict of key/values to force onto every chunk's metadata
            (useful for FinanceBench tagging, e.g. {"financebench_doc_name": "3M_2018_10K"})
    """
    collection = collection_name or settings.QDRANT_COLLECTION
    logger.info(f"Ingesting: {file_path} -> {collection}")

    document = load_pdf(file_path)
    metadata = extract_metadata(file_path, document, doc_type_override=doc_type)
    if metadata_override:
        metadata.update(metadata_override)

    chunks = chunk_document(document, metadata)
    logger.info(f"  Chunked into {len(chunks)} pieces")

    client = get_qdrant_client()
    ensure_collection(client, collection_name=collection)
    upload_chunks(client, chunks, collection_name=collection)

    logger.info(f"  Uploaded {len(chunks)} chunks to Qdrant collection '{collection}'")
    return len(chunks)


def ingest_directory(
    directory: Path,
    doc_type: str | None = None,
    collection_name: str | None = None,
) -> int:
    """Ingest all PDFs in a directory. Returns total chunks uploaded."""
    total = 0
    for pdf_file in sorted(directory.glob("**/*.pdf")):
        total += ingest_file(pdf_file, doc_type=doc_type, collection_name=collection_name)
    logger.info(f"Total chunks ingested from {directory}: {total}")
    return total
