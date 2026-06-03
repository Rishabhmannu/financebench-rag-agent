"""Smoke test — compare HybridChunker vs new markdown-first chunker on 1 PDF.

The HybridChunker path regressed RAGAS because its `chunk.text` serializer
flattens multi-column financial tables into noisy prose. The markdown-first
path exports Docling's clean pipe tables instead.

This script runs BOTH paths on the same DoclingDocument and prints stats +
shows the Cash Flows from Operating Activities chunks from each side by side,
so we can verify by eye that the markdown path produces readable tables.

Usage:
    python scripts/smoke_docling_markdown_chunker.py
    python scripts/smoke_docling_markdown_chunker.py --pdf data/raw/financebench/pdfs/ADOBE_2016_10K.pdf
"""

import argparse
import logging
import sys
import time
from collections import Counter
from pathlib import Path

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
for _n in ("docling", "docling_core", "httpx", "urllib3"):
    logging.getLogger(_n).setLevel(logging.ERROR)

DEFAULT_PDF = "data/raw/financebench/pdfs/3M_2018_10K.pdf"


def _stats(chunks: list[dict], tag: str) -> None:
    if not chunks:
        print(f"  [{tag}] NO CHUNKS")
        return
    lens = [len(c["raw_content"]) for c in chunks]
    have_section = sum(1 for c in chunks if c["metadata"].get("section_header"))
    have_page = sum(1 for c in chunks if c["metadata"].get("page_number") is not None)
    types = Counter(c["metadata"].get("chunk_type", "?") for c in chunks)
    print(f"  [{tag}] n={len(chunks)}  avg_chars={sum(lens) / len(lens):.0f}  (min={min(lens)}, max={max(lens)})")
    print(f"         section_header coverage: {have_section}/{len(chunks)}")
    print(f"         page_number    coverage: {have_page}/{len(chunks)}")
    print(f"         chunk_type dist:         {dict(types)}")


def _find_chunks_containing(chunks: list[dict], needle: str, max_show: int = 2) -> list[dict]:
    hits = [c for c in chunks if needle.lower() in c["raw_content"].lower()]
    return hits[:max_show]


def _show_chunk(c: dict, tag: str) -> None:
    meta = c["metadata"]
    print(f"\n  [{tag}] page={meta.get('page_number')}  type={meta.get('chunk_type')}")
    print(f"         section_header: {meta.get('section_header', '')!r}")
    hp = meta.get("heading_path", "")
    print(f"         heading_path:   {hp[:160]!r}")
    print(f"         raw_content ({len(c['raw_content'])} chars):")
    for line in c["raw_content"].split("\n")[:18]:
        print(f"           {line[:160]}")
    if len(c["raw_content"].split("\n")) > 18:
        print(f"           ... ({len(c['raw_content'].split(chr(10))) - 18} more lines)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", default=DEFAULT_PDF)
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"ERROR: {pdf_path} not found")
        sys.exit(1)

    from docling.document_converter import DocumentConverter

    from src.ingestion.chunker import (
        _chunk_with_docling,
        _chunk_with_docling_markdown,
    )

    print("=" * 80)
    print(f"Chunker A/B smoke test: {pdf_path.name}")
    print("=" * 80)

    t0 = time.time()
    doc = DocumentConverter().convert(str(pdf_path)).document
    print(f"Docling parse: {time.time() - t0:.1f}s")

    metadata = {
        "doc_type": "10k",
        "company": "testco",
        "company_name": "TestCo Inc.",
        "source_file": pdf_path.name,
    }

    t0 = time.time()
    old_chunks = _chunk_with_docling(doc, metadata)
    print(f"HybridChunker: {time.time() - t0:.1f}s, {len(old_chunks)} chunks")

    t0 = time.time()
    new_chunks = _chunk_with_docling_markdown(doc, metadata)
    print(f"Markdown chunker: {time.time() - t0:.1f}s, {len(new_chunks)} chunks")

    print()
    print("-" * 80)
    print("AGGREGATE STATS")
    print("-" * 80)
    _stats(old_chunks, "HybridChunker")
    print()
    _stats(new_chunks, "Markdown chunker")

    # Side-by-side: the Cash Flows from Operating Activities chunk — this is
    # where HybridChunker serialized the Purchases of PP&E row weirdly.
    print()
    print("=" * 80)
    print("SIDE-BY-SIDE: 'Purchases of property' retrieval target")
    print("=" * 80)
    needle = "Purchases of property"
    old_hits = _find_chunks_containing(old_chunks, needle, max_show=1)
    new_hits = _find_chunks_containing(new_chunks, needle, max_show=1)

    print(f"\n>>> HybridChunker hits: {len(old_hits)}")
    for h in old_hits:
        _show_chunk(h, "HybridChunker")
    print(f"\n>>> Markdown chunker hits: {len(new_hits)}")
    for h in new_hits:
        _show_chunk(h, "Markdown chunker")

    # Another key regression case: Cash Flows from Operating (the American Water-style)
    print()
    print("=" * 80)
    print("SIDE-BY-SIDE: 'Cash flows from operating' retrieval target")
    print("=" * 80)
    needle = "Cash flows from operating"
    old_hits = _find_chunks_containing(old_chunks, needle, max_show=1)
    new_hits = _find_chunks_containing(new_chunks, needle, max_show=1)

    print(f"\n>>> HybridChunker hits: {len(old_hits)}")
    for h in old_hits:
        _show_chunk(h, "HybridChunker")
    print(f"\n>>> Markdown chunker hits: {len(new_hits)}")
    for h in new_hits:
        _show_chunk(h, "Markdown chunker")

    # Verdict
    print()
    print("=" * 80)
    print("VERDICT")
    print("=" * 80)
    md_has_pipe_tables = any("|" in c["raw_content"] and c["metadata"].get("chunk_type") == "table" for c in new_chunks)
    md_has_section = all(c["metadata"].get("section_header") for c in new_chunks[-20:])  # tail should have headings
    if md_has_pipe_tables and len(new_chunks) > 0:
        print("  PASS — Markdown chunker produces pipe-table chunks with section metadata.")
        print("         Review the side-by-side above: the 'Purchases of property' chunk")
        print("         from the markdown path should look like a clean pipe table,")
        print("         while the HybridChunker one should be flattened label=value prose.")
        sys.exit(0)
    else:
        print(f"  FAIL — md_has_pipe_tables={md_has_pipe_tables}, n_chunks={len(new_chunks)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
