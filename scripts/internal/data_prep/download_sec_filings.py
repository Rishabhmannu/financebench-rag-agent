"""Stage 1: Download real SEC 10-K filings and extract sections as raw text files.

Uses the edgartools library against SEC EDGAR's public API. Saves each section
to a separate file under data/raw/sec/{TICKER}/10k_{year}/ so Stage 2 can
render PDFs without needing network access.

Usage:
    python scripts/download_sec_filings.py
    python scripts/download_sec_filings.py --companies AAPL MSFT TSLA NVDA
    python scripts/download_sec_filings.py --email "Your Name you@example.com"
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from tqdm import tqdm

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DEFAULT_COMPANIES = ["AAPL", "MSFT", "TSLA"]
DEFAULT_EMAIL = "Rishabh Kumar rishabhkumards07@gmail.com"

# Textual sections (plain strings) and tabular sections (Statement objects needing .to_dataframe())
TEXTUAL_SECTIONS = ["management_discussion", "risk_factors", "business"]
TABULAR_SECTIONS = ["income_statement", "balance_sheet", "cash_flow_statement"]


def download_company(ticker: str, out_root: Path, target_fiscal_year: int | None = None) -> dict:
    """Download a 10-K for a ticker and dump sections to disk.

    If target_fiscal_year is given, picks the filing whose period_of_report
    year matches (fiscal year). Otherwise uses the latest available.

    Returns a meta dict with accession#, filing date, output path.
    """
    from edgar import Company  # imported lazily so --help works without the dep

    company = Company(ticker)
    if target_fiscal_year is None:
        filings = company.get_filings(form="10-K").head(1)
        if not filings:
            raise RuntimeError(f"No 10-K filings found for {ticker}")
        filing = filings[0]
    else:
        # Walk through recent 10-Ks and pick the one whose period_of_report year matches
        all_filings = company.get_filings(form="10-K").head(10)
        filing = None
        for candidate in all_filings:
            period = str(candidate.period_of_report) if candidate.period_of_report else ""
            if period.startswith(str(target_fiscal_year)):
                filing = candidate
                break
        if filing is None:
            available = [str(f.period_of_report)[:4] for f in all_filings if f.period_of_report]
            raise RuntimeError(
                f"No 10-K for {ticker} with fiscal year {target_fiscal_year}. "
                f"Available recent years: {available}"
            )

    # Resolve fiscal year from period_of_report (safer than filing_date for fiscal-year labels)
    tenk = filing.obj()
    fiscal_year = str(tenk.period_of_report)[:4] if tenk.period_of_report else str(filing.filing_date)[:4]

    out_dir = out_root / ticker / f"10k_{fiscal_year}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Textual sections
    for attr in TEXTUAL_SECTIONS:
        content = getattr(tenk, attr, None) or ""
        (out_dir / f"{attr}.txt").write_text(content, encoding="utf-8")

    # Tabular sections — serialize as CSV so they're human-readable
    for attr in TABULAR_SECTIONS:
        stmt = getattr(tenk, attr, None)
        if stmt is None:
            (out_dir / f"{attr}.csv").write_text("", encoding="utf-8")
            continue
        try:
            df = stmt.to_dataframe()
            # Drop xbrl noise columns, keep label + period values
            period_cols = [c for c in df.columns if c not in
                           {"concept", "label", "level", "abstract", "dimension",
                            "balance", "weight", "preferred_sign", "parent_concept"}]
            keep = ["label"] + period_cols
            df[keep].to_csv(out_dir / f"{attr}.csv", index=False)
        except Exception as e:
            logger.warning(f"Failed to serialize {attr} for {ticker}: {e}")
            (out_dir / f"{attr}.csv").write_text("", encoding="utf-8")

    meta = {
        "ticker": ticker,
        "company_name": company.name,
        "accession_number": filing.accession_no,
        "filing_date": str(filing.filing_date),
        "period_of_report": str(filing.period_of_report) if hasattr(filing, "period_of_report") else None,
        "fiscal_year": fiscal_year,
        "form": filing.form,
        "primary_document_url": getattr(filing, "document", None) and filing.document.url or None,
        "sections_saved": TEXTUAL_SECTIONS + TABULAR_SECTIONS,
        "output_dir": str(out_dir),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def main():
    parser = argparse.ArgumentParser(description="Download SEC 10-K filings and extract sections as raw text")
    parser.add_argument("--companies", nargs="+", default=DEFAULT_COMPANIES, help=f"Tickers to download (default: {DEFAULT_COMPANIES})")
    parser.add_argument("--email", default=DEFAULT_EMAIL, help="SEC User-Agent: 'Name email@domain.com' (required by SEC)")
    parser.add_argument("--output-dir", default="data/raw/sec", help="Root directory for extracted sections")
    parser.add_argument("--fiscal-year", type=int, default=None,
                        help="Target fiscal year (period_of_report). Default: latest available.")
    args = parser.parse_args()

    from edgar import set_identity

    set_identity(args.email)
    out_root = Path(args.output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    print(f"Downloading 10-Ks for {len(args.companies)} companies -> {out_root}/")
    print(f"Using SEC identity: {args.email}")
    print()

    manifest = []
    failures = []
    pbar = tqdm(args.companies, desc="Downloading", unit="co", ncols=90)
    for ticker in pbar:
        pbar.set_postfix_str(f"{ticker}")
        try:
            meta = download_company(ticker, out_root, target_fiscal_year=args.fiscal_year)
            manifest.append(meta)
        except Exception as e:
            failures.append((ticker, str(e)))
            tqdm.write(f"  [FAIL] {ticker}: {type(e).__name__}: {e}")
    pbar.close()

    # Write top-level manifest
    (out_root / "manifest.json").write_text(
        json.dumps({"downloaded": manifest, "failures": failures}, indent=2),
        encoding="utf-8",
    )

    print()
    print(f"Success: {len(manifest)}/{len(args.companies)}")
    for m in manifest:
        print(f"  {m['ticker']:6s} FY{m['fiscal_year']}  acc={m['accession_number']}  -> {m['output_dir']}")
    if failures:
        print(f"Failures: {len(failures)}")
        for t, err in failures:
            print(f"  {t}: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
