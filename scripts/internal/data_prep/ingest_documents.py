"""CLI script for batch document ingestion."""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, ".")
from src.ingestion.pipeline import ingest_directory, ingest_file

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def main():
    parser = argparse.ArgumentParser(description="Ingest PDF documents into Qdrant")
    parser.add_argument("--input", required=True, help="Path to a PDF file or directory of PDFs")
    parser.add_argument("--doc-type", default=None, help="Override document type (10k, invoice, expense_policy)")
    parser.add_argument("--collection", default="financial_docs", help="Qdrant collection name")
    args = parser.parse_args()

    path = Path(args.input)
    if path.is_file():
        count = ingest_file(path, doc_type=args.doc_type)
    elif path.is_dir():
        count = ingest_directory(path, doc_type=args.doc_type)
    else:
        print(f"Error: {path} does not exist")
        sys.exit(1)

    print(f"Done. Ingested {count} chunks.")


if __name__ == "__main__":
    main()
