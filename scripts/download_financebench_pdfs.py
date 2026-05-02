"""Fetch the PDFs referenced by FinanceBench's 150 public Q&A set.

Reads `financebench_open_source.jsonl` (must exist — run download_financebench.py
first), figures out the unique set of doc_name values, and downloads each from
Patronus AI's public GitHub raw URL. Skips docs already on disk.

~84 PDFs, ~150-250 MB total. Parallel downloads with a small thread pool.

PDFs are gitignored via the existing `data/raw/` rule. License CC-BY-NC-4.0 —
do not redistribute.

Usage:
    python scripts/download_financebench_pdfs.py
    python scripts/download_financebench_pdfs.py --workers 8
"""

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from tqdm import tqdm

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

FB_DATA_DIR = Path("data/raw/financebench")
FB_QA_PATH = FB_DATA_DIR / "financebench_open_source.jsonl"
PDF_DIR = FB_DATA_DIR / "pdfs"
RAW_URL = "https://raw.githubusercontent.com/patronus-ai/financebench/main/pdfs/{}.pdf"


def _needed_docs() -> list[str]:
    if not FB_QA_PATH.exists():
        print(f"ERROR: {FB_QA_PATH} not found. Run scripts/download_financebench.py first.")
        sys.exit(1)
    names: set[str] = set()
    for line in open(FB_QA_PATH):
        rec = json.loads(line)
        names.add(rec["doc_name"])
        for e in rec.get("evidence", []):
            ev_name = e.get("evidence_doc_name")
            if ev_name:
                names.add(ev_name)
    return sorted(n for n in names if n)


def _download_one(doc_name: str, dst: Path) -> tuple[str, int | None, str | None]:
    if dst.exists() and dst.stat().st_size > 1000:
        return doc_name, 0, None
    try:
        r = requests.get(RAW_URL.format(doc_name), stream=True, timeout=120)
        r.raise_for_status()
        total = 0
        tmp = dst.with_suffix(dst.suffix + ".part")
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    total += len(chunk)
        tmp.rename(dst)
        return doc_name, total, None
    except Exception as e:
        return doc_name, None, str(e)


def main():
    parser = argparse.ArgumentParser(description="Download FinanceBench source PDFs")
    parser.add_argument("--workers", type=int, default=6, help="Parallel download threads")
    args = parser.parse_args()

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    needed = _needed_docs()
    print(f"PDFs needed: {len(needed)}")
    print(f"Output dir:  {PDF_DIR}")
    print()

    failures = []
    already = 0
    downloaded = 0
    total_bytes = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_download_one, name, PDF_DIR / f"{name}.pdf"): name
            for name in needed
        }
        pbar = tqdm(as_completed(futures), total=len(futures), desc="Downloading", unit="pdf", ncols=90)
        for fut in pbar:
            name, n, err = fut.result()
            if err:
                failures.append((name, err))
                tqdm.write(f"  [FAIL] {name}: {err[:100]}")
            elif n == 0:
                already += 1
            else:
                downloaded += 1
                total_bytes += n

    print()
    print(f"Downloaded new: {downloaded}")
    print(f"Already present: {already}")
    print(f"Failed:         {len(failures)}")
    print(f"Bytes fetched:  {total_bytes / (1024 * 1024):.1f} MB")
    if failures:
        print()
        print("Failures (retry by re-running this script):")
        for name, err in failures[:10]:
            print(f"  {name}: {err[:100]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
