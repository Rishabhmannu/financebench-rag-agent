"""Seed Qdrant with sample data for development and testing."""

import argparse
import logging
import sys

sys.path.insert(0, ".")
from pathlib import Path

from src.ingestion.pipeline import ingest_directory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def main():
    parser = argparse.ArgumentParser(description="Seed Qdrant with sample documents")
    parser.add_argument("--sample", action="store_true", help="Use sample data from data/sample/")
    args = parser.parse_args()

    if args.sample:
        sample_dir = Path("data/sample")
        if not sample_dir.exists() or not list(sample_dir.glob("*.pdf")):
            print("No sample PDFs found in data/sample/. Add PDF files there first.")
            sys.exit(1)
        count = ingest_directory(sample_dir)
        print(f"Seeded {count} chunks from sample data.")
    else:
        print("Use --sample to seed from data/sample/")


if __name__ == "__main__":
    main()
