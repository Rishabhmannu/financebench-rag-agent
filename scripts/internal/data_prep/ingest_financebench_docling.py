"""Overnight FinanceBench ingestion with full Docling PDF parsing.

Builds a second Qdrant collection (`financebench_corpus_docling` by default)
alongside the existing pypdf-only `financebench_corpus`, so you can A/B them.

Why run this overnight:
  - Docling parses 10-K PDFs with proper layout analysis (tables, sections,
    footnotes) — the fast pypdf path we used earlier dropped signal critical
    for RAGAS context_recall. Worth the 10x slowdown per PDF.
  - 84 real 10-Ks × ~2.5 min Docling each = ~3-4 hours wall-clock. Not
    interactive-friendly; designed to run while you sleep.

Resilience features for an unattended long run:
  - **Resumable**: queries Qdrant for distinct `financebench_doc_name` values
    already in the collection and skips them. Rerun safe after a kill/crash.
  - **Per-PDF failure handling**: a Docling OOM or PDF parse crash on one
    file won't stop the run. Failures go to `data/raw/financebench/ingest_failures.log`.
  - **Progress bar**: tqdm with current PDF, ETA, rolling average per-PDF time.
  - **Checkpointing**: Qdrant upsert happens per-PDF, so partial progress is
    persisted incrementally.

Usage (from the project root):
    # Foreground (watch the bar)
    python scripts/ingest_financebench_docling.py

    # Overnight with nohup + output to a log you can tail
    nohup python scripts/ingest_financebench_docling.py \
        > /tmp/fb_docling_ingest.log 2>&1 &
    disown

    # Check progress next morning
    tail -f /tmp/fb_docling_ingest.log

    # A/B with a different collection name
    python scripts/ingest_financebench_docling.py --collection financebench_test

    # Resume after a crash — just rerun the same command; already-ingested
    # docs are detected automatically and skipped.
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from qdrant_client.models import Filter, FieldCondition, MatchValue
from tqdm import tqdm

from src.ingestion.pipeline import ingest_file
from src.services.company_registry import canonical_company_slug
from src.services.vector_store import get_qdrant_client

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

FB_DATA_DIR = Path("data/raw/financebench")
FB_DOCS_PATH = FB_DATA_DIR / "financebench_document_information.jsonl"
PDF_DIR = FB_DATA_DIR / "pdfs"
FAILURE_LOG = FB_DATA_DIR / "ingest_failures.log"
DEFAULT_COLLECTION = "financebench_corpus_docling"


def _doc_info_map() -> dict:
    if not FB_DOCS_PATH.exists():
        print(f"ERROR: {FB_DOCS_PATH} missing. Run scripts/download_financebench.py first.")
        sys.exit(1)
    return {json.loads(line)["doc_name"]: json.loads(line) for line in open(FB_DOCS_PATH)}


def _already_ingested(collection: str) -> set[str]:
    """Return the set of `financebench_doc_name` values already present in the
    collection. Used to skip docs that were ingested in a prior run.
    """
    client = get_qdrant_client()
    collections = {c.name for c in client.get_collections().collections}
    if collection not in collections:
        return set()

    # Scroll the entire collection, collecting distinct doc_name payload values.
    # Qdrant doesn't have a native DISTINCT; we paginate and dedupe client-side.
    seen: set[str] = set()
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            limit=500,
            with_payload=["financebench_doc_name"],
            with_vectors=False,
            offset=offset,
        )
        for p in points:
            name = (p.payload or {}).get("financebench_doc_name")
            if name:
                seen.add(name)
        if offset is None:
            break
    return seen


def _log_failure(doc_name: str, err: Exception, fail_path: Path) -> None:
    fail_path.parent.mkdir(parents=True, exist_ok=True)
    with open(fail_path, "a") as f:
        f.write(f"{datetime.utcnow().isoformat()}Z\t{doc_name}\t{type(err).__name__}\t{str(err)[:500]}\n")


def main():
    parser = argparse.ArgumentParser(description="Overnight Docling ingestion of FinanceBench PDFs")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION, help="Qdrant collection name")
    parser.add_argument("--limit", type=int, default=None, help="Only ingest first N PDFs (smoke-test)")
    parser.add_argument("--force", action="store_true", help="Re-ingest even if doc already in collection")
    args = parser.parse_args()

    # Critical: ensure SKIP_DOCLING is NOT set — we want Docling's full layout
    # analysis for this run.
    if os.environ.get("SKIP_DOCLING") == "1":
        print("ERROR: SKIP_DOCLING=1 is set. Unset it before running this script.")
        print("  This script specifically needs Docling for proper table/layout parsing.")
        sys.exit(1)

    if not PDF_DIR.exists():
        print(f"ERROR: {PDF_DIR} missing. Run scripts/download_financebench_pdfs.py first.")
        sys.exit(1)

    doc_info = _doc_info_map()
    all_pdfs = sorted(PDF_DIR.glob("*.pdf"))
    if args.limit:
        all_pdfs = all_pdfs[: args.limit]

    # Resumability: skip what's already in the target collection
    if args.force:
        already = set()
    else:
        print("Checking collection for already-ingested docs...")
        already = _already_ingested(args.collection)
        if already:
            print(f"  Found {len(already)} already-ingested doc(s); will skip unless --force")

    remaining = [p for p in all_pdfs if p.stem not in already]

    print()
    print(f"Target collection: {args.collection}")
    print(f"PDFs total:        {len(all_pdfs)}")
    print(f"Already ingested:  {len(all_pdfs) - len(remaining)}")
    print(f"To process:        {len(remaining)}")
    print(f"Failure log:       {FAILURE_LOG}")
    print(f"Est. wall-clock:   ~{len(remaining) * 2.5:.0f} min (avg 2.5 min/PDF with Docling)")
    print()

    if not remaining:
        print("Nothing to do. All PDFs already ingested in this collection.")
        return

    total_chunks = 0
    failures: list[tuple[str, str]] = []
    start_time = time.time()

    pbar = tqdm(remaining, desc="Docling ingest", unit="pdf", ncols=100)
    for pdf_path in pbar:
        doc_name = pdf_path.stem
        pbar.set_postfix_str(doc_name[:40])
        info = doc_info.get(doc_name, {})
        doc_type_raw = (info.get("doc_type") or "10k").lower()
        doc_type = doc_type_raw if doc_type_raw in ("10k", "10q", "8k") else "10k"

        metadata_override = {
            "financebench_doc_name": doc_name,
            "fb_company": info.get("company", "unknown"),
            "fb_doc_period": str(info.get("doc_period", "")),
            "fb_gics_sector": info.get("gics_sector", "unknown"),
            "company": canonical_company_slug(info.get("company")) or "unknown",
        }
        if str(info.get("doc_period", "")).isdigit():
            metadata_override["fiscal_year"] = int(info.get("doc_period"))

        pdf_start = time.time()
        try:
            n = ingest_file(
                pdf_path,
                doc_type=doc_type,
                collection_name=args.collection,
                metadata_override=metadata_override,
            )
            total_chunks += n
            pdf_elapsed = time.time() - pdf_start
            tqdm.write(f"  [OK]   {doc_name}  ->  {n} chunks  ({pdf_elapsed:.0f}s)")
        except Exception as e:
            failures.append((doc_name, f"{type(e).__name__}: {str(e)[:200]}"))
            _log_failure(doc_name, e, FAILURE_LOG)
            tqdm.write(f"  [FAIL] {doc_name}: {type(e).__name__}: {str(e)[:100]}")

    pbar.close()

    elapsed_min = (time.time() - start_time) / 60
    print()
    print("=" * 80)
    print(f"Processed:     {len(remaining) - len(failures)}/{len(remaining)}")
    print(f"Total chunks:  {total_chunks}")
    print(f"Wall-clock:    {elapsed_min:.1f} min")
    print(f"Failures:      {len(failures)}")
    if failures:
        print(f"Failure log:   {FAILURE_LOG}")
        print("First 5 failures:")
        for name, err in failures[:5]:
            print(f"  {name}: {err[:100]}")
        print()
        print("To retry failed docs, re-run this script — resumable mode will skip")
        print("the OK docs and retry the failures.")
    print("=" * 80)


if __name__ == "__main__":
    main()
