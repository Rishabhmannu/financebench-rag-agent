"""Fetch Patronus AI's FinanceBench dataset (JSONL + optional PDFs).

Two artifacts:
  - financebench_open_source.jsonl  — 150 Q&A records
  - financebench_document_information.jsonl — 361 doc metadata records

Both live in data/raw/financebench/ (gitignored via data/raw/ rule).

The accompanying PDFs (~672 MB total) are NOT downloaded by default — Phase B
of the FinanceBench integration downloads them on demand per-company. Phase A
(proof-of-life) can run against our existing Qdrant collection for questions
about companies we already have (Microsoft overlaps).

License note: FinanceBench is released CC-BY-NC-4.0 on HuggingFace (HF dataset
card authoritative; the GitHub repo has no LICENSE file). We do not
redistribute — users fetch it themselves via this script. Cite Patronus AI
(arXiv 2311.11944) in any public writeup.

Usage:
    python scripts/download_financebench.py
    python scripts/download_financebench.py --with-pdfs apple msft tsla
"""

import argparse
import logging
import sys
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

REPO_RAW = "https://raw.githubusercontent.com/patronus-ai/financebench/main"
OPEN_SOURCE_JSONL = f"{REPO_RAW}/data/financebench_open_source.jsonl"
DOC_INFO_JSONL = f"{REPO_RAW}/data/financebench_document_information.jsonl"


def _download(url: str, dst: Path) -> int:
    """Stream a URL to disk. Returns byte count."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    total = 0
    with open(dst, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)
                total += len(chunk)
    return total


def main():
    parser = argparse.ArgumentParser(description="Download FinanceBench JSONL artifacts")
    parser.add_argument(
        "--output-dir",
        default="data/raw/financebench",
        help="Where to save the JSONL files (gitignored).",
    )
    args = parser.parse_args()

    out_dir = Path(args.output_dir)

    files = [
        (OPEN_SOURCE_JSONL, out_dir / "financebench_open_source.jsonl"),
        (DOC_INFO_JSONL, out_dir / "financebench_document_information.jsonl"),
    ]

    for url, dst in files:
        if dst.exists():
            logger.info(f"Already present: {dst} ({dst.stat().st_size} bytes)")
            continue
        logger.info(f"Downloading {url}")
        try:
            n = _download(url, dst)
            logger.info(f"  Saved {n} bytes to {dst}")
        except requests.HTTPError as e:
            logger.error(f"  Failed: {e}")
            sys.exit(1)

    # Quick summary so the user can sanity-check
    qa_path = out_dir / "financebench_open_source.jsonl"
    doc_path = out_dir / "financebench_document_information.jsonl"
    qa_count = sum(1 for _ in open(qa_path))
    doc_count = sum(1 for _ in open(doc_path))
    print()
    print(f"Q&A records:  {qa_count}")
    print(f"Doc records:  {doc_count}")
    print(f"Saved to:     {out_dir}/")


if __name__ == "__main__":
    main()
