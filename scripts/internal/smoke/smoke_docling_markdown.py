"""Smoke test for Docling's markdown export — do financial tables render cleanly?

This is the 2-minute decision gate before committing to a markdown-first
chunker overhaul. We're testing ONE thing: when Docling converts a dense
multi-column financial statement (e.g. Consolidated Statements of Income)
to markdown, does it produce a clean pipe table like

    | Revenue      | 2018 | 2017 |
    | Subscription | 4584 | 3223 |

...or does it produce the same mangled "label, col = value" text that
HybridChunker's serializer produced?

If pipe tables are clean → worth building a markdown-aware chunker.
If they're still garbled → Docling's fundamental table export is the issue,
no chunking strategy saves us, roll back to pypdf.

Usage:
    python scripts/smoke_docling_markdown.py
    python scripts/smoke_docling_markdown.py --pdf data/raw/financebench/pdfs/ADOBE_2016_10K.pdf
"""

import argparse
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
for _n in ("docling", "docling_core", "httpx", "urllib3"):
    logging.getLogger(_n).setLevel(logging.ERROR)

DEFAULT_PDF = "data/raw/financebench/pdfs/3M_2018_10K.pdf"
MD_OUT_DIR = Path("/tmp")


def _print_table_block(lines: list[str], start: int, tag: str, max_rows: int = 12) -> None:
    """Print a contiguous run of markdown table rows (lines starting with '|')."""
    end = start
    while end < len(lines) and lines[end].lstrip().startswith("|"):
        end += 1
    print(f"\n--- {tag} — markdown table at line {start} ({end - start} rows) ---")
    for ln in lines[start : min(start + max_rows, end)]:
        print(f"  {ln.rstrip()}")
    if end - start > max_rows:
        print(f"  ... ({end - start - max_rows} more rows)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", default=DEFAULT_PDF)
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"ERROR: {pdf_path} not found")
        sys.exit(1)

    print("=" * 80)
    print(f"Docling MARKDOWN smoke test: {pdf_path.name}")
    print(f"PDF size: {pdf_path.stat().st_size / 1024:.0f} KB")
    print("=" * 80)

    # Parse
    from docling.document_converter import DocumentConverter

    t0 = time.time()
    doc = DocumentConverter().convert(str(pdf_path)).document
    print(f"\nDocling parse: {time.time() - t0:.1f}s")

    # Export to markdown
    t0 = time.time()
    md = doc.export_to_markdown()
    print(f"export_to_markdown: {time.time() - t0:.1f}s")
    print(f"Total markdown chars: {len(md):,}")
    lines = md.split("\n")
    print(f"Total lines: {len(lines):,}")

    # Structural markers
    heading_lines = [(i, ln) for i, ln in enumerate(lines) if ln.startswith("#")]
    table_row_lines = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("|")]
    print(f"Heading lines (# ... ######): {len(heading_lines)}")
    print(f"Table row lines (start with '|'): {len(table_row_lines)}")

    # Save full markdown for manual inspection
    out_path = MD_OUT_DIR / f"{pdf_path.stem}.md"
    out_path.write_text(md)
    print(f"Full markdown saved: {out_path}")

    # --- Show the first 5 headings ---
    print("\n" + "=" * 80)
    print("FIRST 10 HEADINGS")
    print("=" * 80)
    for i, (ln_idx, ln) in enumerate(heading_lines[:10], 1):
        print(f"  [{i}] line {ln_idx}: {ln[:120]}")

    # --- Show the first 3 contiguous table blocks ---
    print("\n" + "=" * 80)
    print("TABLE RENDERING CHECK")
    print("=" * 80)
    if not table_row_lines:
        print("\n  NO markdown-style tables found (no lines start with '|').")
        print("  This is the FAIL signal — Docling serialized tables as prose.")
        sys.exit(1)

    shown = 0
    seen_starts = set()
    i = 0
    while i < len(table_row_lines) and shown < 3:
        start = table_row_lines[i]
        if start in seen_starts:
            i += 1
            continue
        # advance i past this whole block
        end = start
        while end < len(lines) and lines[end].lstrip().startswith("|"):
            seen_starts.add(end)
            end += 1
        _print_table_block(lines, start, tag=f"TABLE {shown + 1}")
        shown += 1
        i += 1

    # --- Search for the Adobe-regression-style content: a Consolidated Statements table ---
    print("\n" + "=" * 80)
    print("KEY-SECTION INSPECTION (the kind that regressed in HybridChunker)")
    print("=" * 80)
    needles = [
        "Consolidated Statements of Income",
        "Consolidated Balance Sheet",
        "Cash Flows from Operating",
        "Property, Plant and Equipment",
        "Capital expenditure",
        "Purchases of property",
    ]
    found_any = False
    for needle in needles:
        for idx, ln in enumerate(lines):
            if needle.lower() in ln.lower():
                found_any = True
                print(f"\n>>> Found '{needle}' at line {idx}: {ln.strip()[:100]}")
                # Show ~20 lines of context (headings + following table)
                for ln2 in lines[idx : min(idx + 20, len(lines))]:
                    print(f"    {ln2.rstrip()[:140]}")
                break  # first match per needle is enough

    if not found_any:
        print("  (none of the expected section keywords matched — weird, check the saved .md)")

    # --- VERDICT ---
    print("\n" + "=" * 80)
    print("VERDICT")
    print("=" * 80)
    if len(table_row_lines) > 50:
        print(f"  PASS — {len(table_row_lines)} table-row lines present. Tables appear to render as pipe tables.")
        print(f"         Open {out_path} to verify the key sections look clean.")
        print("         If they do, worth building a markdown-aware chunker for overnight re-ingest.")
    else:
        print(f"  SOFT FAIL — only {len(table_row_lines)} table-row lines. Docling exported tables as prose, not pipe tables.")
        print("         Markdown-first approach will not help — roll back to pypdf.")


if __name__ == "__main__":
    main()
