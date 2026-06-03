"""Ingest FinanceBench PDFs into an isolated Qdrant collection.

Reads `data/raw/financebench/financebench_document_information.jsonl` to map
each downloaded PDF to its FinanceBench metadata (doc_name, doc_type,
doc_period, company, gics_sector), then runs our standard ingestion pipeline
(Docling → chunker with contextual prefix → Qdrant) against a separate
collection `financebench_corpus`.

Each chunk's Qdrant payload carries the extra fields:
  - financebench_doc_name  (e.g. "3M_2018_10K") — lets the eval correlate
    an answer's source back to the expected evidence doc
  - fb_company             (canonical FB company name)
  - fb_doc_period          (year or period label)
  - fb_gics_sector         (top-level industry classification)

Usage:
    python scripts/ingest_financebench.py
    python scripts/ingest_financebench.py --collection financebench_corpus
    python scripts/ingest_financebench.py --limit 5    # smoke test subset
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from tqdm import tqdm

from src.ingestion.pipeline import ingest_file
from src.services.company_registry import canonical_company_slug
from src.services.vector_store import get_qdrant_client

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

FB_DATA_DIR = Path("data/raw/financebench")
FB_DOCS_PATH = FB_DATA_DIR / "financebench_document_information.jsonl"
PDF_DIR = FB_DATA_DIR / "pdfs"
DEFAULT_COLLECTION = "financebench_corpus"
FAILURE_LOG = FB_DATA_DIR / "ingest_failures_pypdf.log"


def _doc_info_map() -> dict:
    if not FB_DOCS_PATH.exists():
        print(f"ERROR: {FB_DOCS_PATH} missing. Run scripts/download_financebench.py first.")
        sys.exit(1)
    return {json.loads(line)["doc_name"]: json.loads(line) for line in open(FB_DOCS_PATH)}


def _already_ingested(collection: str) -> set[str]:
    """Return already ingested `financebench_doc_name` values for resumability."""
    client = get_qdrant_client()
    collections = {c.name for c in client.get_collections().collections}
    if collection not in collections:
        return set()

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
    parser = argparse.ArgumentParser(description="Ingest FinanceBench PDFs into an isolated Qdrant collection")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION, help="Qdrant collection name")
    parser.add_argument("--limit", type=int, default=None, help="Only ingest first N PDFs (smoke-test)")
    parser.add_argument("--force", action="store_true", help="Re-ingest even if doc already exists in collection")
    args = parser.parse_args()

    if not PDF_DIR.exists():
        print(f"ERROR: {PDF_DIR} missing. Run scripts/download_financebench_pdfs.py first.")
        sys.exit(1)

    doc_info = _doc_info_map()

    # Find PDFs that match what's in our Q&A set (scoped to the ones we downloaded)
    pdfs = sorted(PDF_DIR.glob("*.pdf"))
    if args.limit:
        pdfs = pdfs[: args.limit]

    if args.force:
        already = set()
    else:
        print("Checking collection for already-ingested docs...")
        already = _already_ingested(args.collection)
        if already:
            print(f"  Found {len(already)} already-ingested doc(s); will skip unless --force")
    remaining = [p for p in pdfs if p.stem not in already]

    print(f"Ingesting into Qdrant collection '{args.collection}'")
    print(f"PDFs total:       {len(pdfs)}")
    print(f"Already ingested: {len(pdfs) - len(remaining)}")
    print(f"To process:       {len(remaining)}")
    print(f"Failure log:      {FAILURE_LOG}")
    print()

    total_chunks = 0
    failures = []
    start_time = time.time()
    pbar = tqdm(remaining, desc="Ingesting", unit="pdf", ncols=90)
    for pdf_path in pbar:
        doc_name = pdf_path.stem  # e.g. "3M_2018_10K"
        pbar.set_postfix_str(doc_name)
        info = doc_info.get(doc_name, {})
        doc_type_raw = info.get("doc_type", "10k")
        # Normalize FB doc_type ("10k"/"10q"/"8k"/"Earnings") → slugs our metadata accepts
        doc_type = doc_type_raw.lower() if doc_type_raw in ("10k", "10q", "8k") else "10k_annualreport" if doc_type_raw == "10k_annualreport" else "10k"

        # Metadata override — everything FB-specific lives here so the eval runner can filter
        metadata_override = {
            "financebench_doc_name": doc_name,
            "fb_company": info.get("company", "unknown"),
            "fb_doc_period": str(info.get("doc_period", "")),
            "fb_gics_sector": info.get("gics_sector", "unknown"),
            "company": canonical_company_slug(info.get("company")) or "unknown",
        }
        if str(info.get("doc_period", "")).isdigit():
            metadata_override["fiscal_year"] = int(info.get("doc_period"))
        try:
            n = ingest_file(
                pdf_path,
                doc_type=doc_type,
                collection_name=args.collection,
                metadata_override=metadata_override,
            )
            total_chunks += n
        except Exception as e:
            failures.append((doc_name, str(e)))
            _log_failure(doc_name, e, FAILURE_LOG)
            tqdm.write(f"  [FAIL] {doc_name}: {type(e).__name__}: {str(e)[:120]}")

    print()
    elapsed_min = (time.time() - start_time) / 60
    print(f"Total chunks uploaded: {total_chunks}")
    print(f"Collection:            {args.collection}")
    print(f"Wall-clock:            {elapsed_min:.1f} min")
    print(f"Failures:              {len(failures)}")
    if failures:
        for name, err in failures[:10]:
            print(f"  {name}: {err[:100]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
