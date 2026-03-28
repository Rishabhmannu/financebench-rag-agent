"""Main document ingestion orchestrator: PDF -> Docling -> Chunk -> Embed -> Qdrant."""

import logging
from pathlib import Path

from src.ingestion.chunker import chunk_document
from src.ingestion.docling_loader import load_pdf
from src.ingestion.metadata_extractor import extract_metadata
from src.ingestion.qdrant_uploader import upload_chunks
from src.services.vector_store import ensure_collection, get_qdrant_client

logger = logging.getLogger(__name__)


def ingest_file(file_path: Path, doc_type: str | None = None) -> int:
    """Ingest a single PDF file. Returns number of chunks uploaded."""
    logger.info(f"Ingesting: {file_path}")

    # Step 1: Load and convert PDF
    document = load_pdf(file_path)

    # Step 2: Extract metadata
    metadata = extract_metadata(file_path, document, doc_type_override=doc_type)

    # Step 3: Chunk
    chunks = chunk_document(document, metadata)
    logger.info(f"  Chunked into {len(chunks)} pieces")

    # Step 4: Upload to Qdrant
    client = get_qdrant_client()
    ensure_collection(client)
    upload_chunks(client, chunks)

    logger.info(f"  Uploaded {len(chunks)} chunks to Qdrant")
    return len(chunks)


def ingest_directory(directory: Path, doc_type: str | None = None) -> int:
    """Ingest all PDFs in a directory. Returns total chunks uploaded."""
    total = 0
    for pdf_file in sorted(directory.glob("**/*.pdf")):
        total += ingest_file(pdf_file, doc_type=doc_type)
    logger.info(f"Total chunks ingested from {directory}: {total}")
    return total
