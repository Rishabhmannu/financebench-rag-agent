"""Seed Qdrant with PDF documents — sample corpus or your own."""

import argparse
import logging
import sys

sys.path.insert(0, ".")
from pathlib import Path

from src.ingestion.pipeline import ingest_directory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def main():
    parser = argparse.ArgumentParser(
        description="Seed Qdrant with PDF documents (sample corpus or your own folder).",
        epilog=(
            "Examples:\n"
            "  python scripts/seed_qdrant.py --sample\n"
            "  python scripts/seed_qdrant.py --dir ~/my-finance-pdfs/\n"
            "  python scripts/seed_qdrant.py --dir ~/acme-q3/ --collection acme_q3_2026\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="Use sample data from data/sample/ (the 8-PDF demo corpus).",
    )
    # 0.1.8: --dir + --collection let technical users ingest their own PDFs
    # into a custom Qdrant collection. The underlying ingest_directory()
    # already accepted both parameters; this just exposes them at the CLI.
    # Performance caveat: the LoRA-FT reranker + tuned prompts are
    # FinanceBench-specific; pass-rate on non-FB corpora may differ from the
    # 72.7% headline. Suitable for personal use / private docs, not eval.
    parser.add_argument(
        "--dir",
        type=Path,
        metavar="PATH",
        help="Ingest PDFs from a directory (recursively). Use for your own corpus.",
    )
    parser.add_argument(
        "--collection",
        type=str,
        metavar="NAME",
        help=(
            "Target Qdrant collection. Defaults to the QDRANT_COLLECTION env value "
            "(typically `financial_docs`). Set this to keep custom corpora "
            "separate from the demo / eval collections."
        ),
    )
    args = parser.parse_args()

    if args.sample and args.dir:
        print("--sample and --dir are mutually exclusive; pick one.")
        sys.exit(2)

    if args.sample:
        source_dir = Path("data/sample")
        label = "sample"
    elif args.dir:
        source_dir = args.dir.expanduser().resolve()
        label = str(source_dir)
    else:
        print("Use --sample to seed from data/sample/, or --dir <path> for your own PDFs.")
        sys.exit(2)

    if not source_dir.exists():
        print(f"Directory not found: {source_dir}")
        sys.exit(1)
    pdfs = list(source_dir.glob("**/*.pdf"))
    if not pdfs:
        print(f"No PDF files found under {source_dir}. (Searched recursively.)")
        sys.exit(1)

    print(f"Ingesting {len(pdfs)} PDF(s) from {source_dir} ...")
    if args.collection:
        print(f"Target collection: {args.collection}")
    count = ingest_directory(source_dir, collection_name=args.collection)
    coll_suffix = f" into '{args.collection}'" if args.collection else ""
    print(f"Seeded {count} chunks from {label}{coll_suffix}.")


if __name__ == "__main__":
    main()
