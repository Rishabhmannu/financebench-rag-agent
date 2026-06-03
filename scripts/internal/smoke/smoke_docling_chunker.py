"""Smoke test for the Docling HybridChunker integration.

Runs both chunking paths (old pypdf per-page + new Docling HybridChunker)
against ONE PDF and prints side-by-side stats so we can verify Docling's
output is actually being used before committing to a 3-hour overnight
re-ingestion.

Usage:
    python scripts/smoke_docling_chunker.py
    python scripts/smoke_docling_chunker.py --pdf data/raw/financebench/pdfs/3M_2018_10K.pdf

Exits non-zero if the two paths produce identical output (which would mean
Docling is silently being discarded — the bug we're fixing).
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
# Quiet noisy inner loggers
for _n in ("docling", "docling_core", "httpx", "urllib3"):
    logging.getLogger(_n).setLevel(logging.ERROR)

DEFAULT_PDF = "data/raw/financebench/pdfs/3M_2018_10K.pdf"


def _dump_sample(chunks: list[dict], n: int = 3) -> None:
    print(f"  Total chunks: {len(chunks)}")
    # Aggregate stats
    lengths = [len(c["raw_content"]) for c in chunks]
    avg_len = sum(lengths) / len(lengths) if lengths else 0
    print(f"  Avg chunk chars: {avg_len:.0f}  (min={min(lengths, default=0)}, max={max(lengths, default=0)})")
    # Structural metadata presence
    have_section = sum(1 for c in chunks if c["metadata"].get("section_header"))
    have_heading = sum(1 for c in chunks if c["metadata"].get("heading_path"))
    have_page = sum(1 for c in chunks if c["metadata"].get("page_number") is not None)
    table_chunks = sum(1 for c in chunks if c["metadata"].get("chunk_type") == "table")
    print(f"  With section_header: {have_section}")
    print(f"  With heading_path:   {have_heading}")
    print(f"  With page_number:    {have_page}")
    print(f"  chunk_type=table:    {table_chunks}")
    print()
    print(f"  --- First {n} chunks (raw_content preview + metadata) ---")
    for i, c in enumerate(chunks[:n], 1):
        raw = c["raw_content"]
        meta = c["metadata"]
        section = meta.get("section_header", "")
        heading_path = meta.get("heading_path", "")
        page = meta.get("page_number")
        ctype = meta.get("chunk_type", "text")
        print(f"\n  [{i}] page={page}  type={ctype}  section={section!r}")
        if heading_path:
            print(f"      heading_path: {heading_path}")
        print(f"      text({len(raw)} chars): {raw[:300]!r}...")


def _run_pypdf_path(pdf_path: Path) -> list[dict]:
    """Force the old pypdf per-page path by setting SKIP_DOCLING=1."""
    print("Loading PDF with SKIP_DOCLING=1 (pypdf per-page, old path)...")
    os.environ["SKIP_DOCLING"] = "1"
    try:
        # Re-import cleanly to pick up the env var (loader reads it at call time so re-import not strictly needed)
        from src.ingestion.docling_loader import load_pdf
        from src.ingestion.chunker import chunk_document

        t0 = time.time()
        doc = load_pdf(pdf_path)
        print(f"  load_pdf took {time.time() - t0:.1f}s")
        print(f"  docling_doc present: {doc.get('docling_doc') is not None} (expected False with SKIP_DOCLING=1)")
        metadata = {
            "doc_type": "10k",
            "company": "testco",
            "company_name": "TestCo Inc.",
            "source_file": pdf_path.name,
            "num_pages": doc.get("num_pages", 0),
        }
        t0 = time.time()
        chunks = chunk_document(doc, metadata)
        print(f"  chunk_document took {time.time() - t0:.1f}s")
        return chunks
    finally:
        os.environ.pop("SKIP_DOCLING", None)


def _run_docling_path(pdf_path: Path) -> list[dict]:
    """Normal path — Docling runs, HybridChunker is invoked."""
    print("Loading PDF with Docling + HybridChunker (new path)...")
    from src.ingestion.docling_loader import load_pdf
    from src.ingestion.chunker import chunk_document

    t0 = time.time()
    doc = load_pdf(pdf_path)
    print(f"  load_pdf took {time.time() - t0:.1f}s")
    print(f"  docling_doc present: {doc.get('docling_doc') is not None} (expected True)")
    if doc.get("docling_doc") is None:
        print("  WARNING: Docling returned None — HybridChunker will NOT be used.")
    metadata = {
        "doc_type": "10k",
        "company": "testco",
        "company_name": "TestCo Inc.",
        "source_file": pdf_path.name,
        "num_pages": doc.get("num_pages", 0),
    }
    t0 = time.time()
    chunks = chunk_document(doc, metadata)
    print(f"  chunk_document took {time.time() - t0:.1f}s")
    return chunks


def main():
    parser = argparse.ArgumentParser(description="Smoke test: Docling HybridChunker vs pypdf path")
    parser.add_argument("--pdf", default=DEFAULT_PDF, help="Path to a single PDF to compare against")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"ERROR: {pdf_path} not found. Run scripts/download_financebench_pdfs.py first,")
        print("or pass --pdf data/sample/10k_aapl_2023.pdf to use an existing sample.")
        sys.exit(1)

    print("=" * 80)
    print(f"Smoke test on: {pdf_path}")
    print(f"PDF size: {pdf_path.stat().st_size / 1024:.0f} KB")
    print("=" * 80)
    print()

    # OLD path: pypdf per-page
    old_chunks = _run_pypdf_path(pdf_path)
    print()
    print("----- OLD PATH (pypdf per-page) -----")
    _dump_sample(old_chunks)
    print()

    # NEW path: Docling HybridChunker
    new_chunks = _run_docling_path(pdf_path)
    print()
    print("----- NEW PATH (Docling HybridChunker) -----")
    _dump_sample(new_chunks)
    print()

    # Verdict: the two paths MUST produce different output if the fix worked
    print("=" * 80)
    print("VERDICT")
    print("=" * 80)
    different = len(old_chunks) != len(new_chunks) or (
        old_chunks and new_chunks and old_chunks[0]["raw_content"] != new_chunks[0]["raw_content"]
    )
    has_structural_meta = any(
        c["metadata"].get("section_header") or c["metadata"].get("heading_path") or c["metadata"].get("chunk_type") == "table"
        for c in new_chunks
    )

    print(f"  Old path chunk count: {len(old_chunks)}")
    print(f"  New path chunk count: {len(new_chunks)}")
    print(f"  Outputs differ:       {different}")
    print(f"  Structural metadata:  {has_structural_meta} (section_header / heading_path / table)")
    print()

    if different and has_structural_meta:
        print("  PASS — HybridChunker is actually being used and producing structured chunks.")
        print("         Safe to commit to the overnight re-ingestion.")
        sys.exit(0)
    else:
        print("  FAIL — Docling output does not appear to be driving the new path.")
        if not different:
            print("         Chunk output is identical to the old path (Docling still silently discarded).")
        if not has_structural_meta:
            print("         No chunks have section_header / heading_path / table_type metadata.")
        sys.exit(1)


if __name__ == "__main__":
    main()
