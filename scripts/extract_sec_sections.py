"""Stage 2: Read raw SEC section files from data/raw/sec/ and render each
company's selected sections to a single PDF in data/sample/.

Uses reportlab (robust multi-page layout). No network required — reads only from
the files Stage 1 produced. Output filenames match the synthetic ones so
seed_qdrant.py picks them up without changes.

Usage:
    python scripts/extract_sec_sections.py
    python scripts/extract_sec_sections.py --sections management_discussion income_statement balance_sheet
    python scripts/extract_sec_sections.py --input-dir data/raw/sec --output-dir data/sample
"""

import argparse
import csv
import json
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from tqdm import tqdm

DEFAULT_SECTIONS = [
    "management_discussion",
    "income_statement",
    "balance_sheet",
    "cash_flow_statement",
]

SECTION_TITLES = {
    "management_discussion": "Management's Discussion and Analysis",
    "risk_factors": "Risk Factors",
    "business": "Business Overview",
    "income_statement": "Consolidated Statements of Operations",
    "balance_sheet": "Consolidated Balance Sheets",
    "cash_flow_statement": "Consolidated Statements of Cash Flows",
}


def _styles():
    ss = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("Title", parent=ss["Title"], fontSize=22, spaceAfter=20, alignment=1),
        "subtitle": ParagraphStyle("Subtitle", parent=ss["Normal"], fontSize=14, spaceAfter=8, alignment=1),
        "meta": ParagraphStyle("Meta", parent=ss["Italic"], fontSize=9, spaceAfter=4, alignment=1),
        "section_heading": ParagraphStyle("H1", parent=ss["Heading1"], fontSize=16, spaceAfter=12),
        "body": ParagraphStyle("Body", parent=ss["BodyText"], fontSize=10, leading=13, spaceAfter=6),
    }


def _escape(text: str) -> str:
    """Escape HTML-ish characters reportlab Paragraph treats as markup."""
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


def cover_page(styles: dict, meta: dict) -> list:
    company_name = meta.get("company_name") or meta["ticker"]
    fiscal_year = meta["fiscal_year"]
    return [
        Spacer(1, 2 * inch),
        Paragraph(_escape(company_name), styles["title"]),
        Paragraph("Form 10-K", styles["subtitle"]),
        Paragraph(f"Fiscal Year {fiscal_year}", styles["subtitle"]),
        Spacer(1, 0.3 * inch),
        Paragraph(f"Source: SEC EDGAR (accession {meta['accession_number']})", styles["meta"]),
        Paragraph(f"Filing date: {meta['filing_date']}", styles["meta"]),
        PageBreak(),
    ]


def text_section(styles: dict, title: str, text: str) -> list:
    story: list = [Paragraph(_escape(title), styles["section_heading"])]
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            story.append(Spacer(1, 0.08 * inch))
            continue
        story.append(Paragraph(_escape(para), styles["body"]))
    story.append(PageBreak())
    return story


def csv_section(styles: dict, title: str, csv_path: Path) -> list:
    story: list = [Paragraph(_escape(title), styles["section_heading"])]
    with csv_path.open(encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        story.append(Paragraph("(no data)", styles["body"]))
        story.append(PageBreak())
        return story

    # Cap at first label + up to 4 period columns to keep the table width sane
    header, *data = rows
    cols = len(header)

    # Convert all cells to Paragraphs so they wrap within each table cell
    cell_style = ParagraphStyle(
        "Cell", parent=styles["body"], fontSize=8, leading=10, spaceAfter=0
    )
    header_style = ParagraphStyle(
        "CellHdr", parent=cell_style, fontName="Helvetica-Bold"
    )

    table_data = [[Paragraph(_escape(str(c)), header_style) for c in header]]
    for row in data:
        row_padded = (row + [""] * cols)[:cols]
        table_data.append([Paragraph(_escape(str(c)), cell_style) for c in row_padded])

    # Column widths: first col (label) wider, rest split evenly
    page_w = letter[0] - 1 * inch  # account for 0.5" margins both sides
    label_w = page_w * 0.45
    other_w = (page_w - label_w) / max(cols - 1, 1)
    col_widths = [label_w] + [other_w] * (cols - 1)

    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(table)
    story.append(PageBreak())
    return story


def render_company(meta: dict, sections: list[str], output_dir: Path) -> Path:
    source_dir = Path(meta["output_dir"])
    ticker = meta["ticker"]
    fiscal_year = meta["fiscal_year"]

    styles = _styles()
    story: list = cover_page(styles, meta)

    for sec in sections:
        title = SECTION_TITLES.get(sec, sec.replace("_", " ").title())
        txt_path = source_dir / f"{sec}.txt"
        csv_path = source_dir / f"{sec}.csv"
        if txt_path.exists() and txt_path.stat().st_size > 0:
            story.extend(text_section(styles, title, txt_path.read_text(encoding="utf-8")))
        elif csv_path.exists() and csv_path.stat().st_size > 0:
            story.extend(csv_section(styles, title, csv_path))

    output_path = output_dir / f"10k_{ticker.lower()}_{fiscal_year}.pdf"
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
        title=f"{meta.get('company_name', ticker)} 10-K FY{fiscal_year}",
    )
    doc.build(story)
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Render SEC sections to per-company PDFs")
    parser.add_argument("--input-dir", default="data/raw/sec", help="Root directory from Stage 1")
    parser.add_argument("--output-dir", default="data/sample", help="Where to write PDFs (replaces synthetic ones)")
    parser.add_argument("--sections", nargs="+", default=DEFAULT_SECTIONS,
                        help=f"Sections to include. Default: {DEFAULT_SECTIONS}. "
                             f"Options: {list(SECTION_TITLES.keys())}")
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = input_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"ERROR: no manifest at {manifest_path} — run scripts/download_sec_filings.py first")
        return 1

    manifest = json.loads(manifest_path.read_text())
    downloaded = manifest.get("downloaded", [])
    if not downloaded:
        print("ERROR: manifest has no downloaded filings")
        return 1

    print(f"Rendering {len(downloaded)} companies with sections: {args.sections}")
    print(f"Output -> {output_dir}/")
    print()

    pbar = tqdm(downloaded, desc="Rendering PDFs", unit="co", ncols=90)
    outputs = []
    for meta in pbar:
        pbar.set_postfix_str(meta["ticker"])
        try:
            out = render_company(meta, args.sections, output_dir)
            outputs.append(out)
        except Exception as e:
            tqdm.write(f"  [FAIL] {meta['ticker']}: {type(e).__name__}: {e}")
    pbar.close()

    print()
    print(f"Rendered {len(outputs)} PDFs:")
    for p in outputs:
        size_kb = p.stat().st_size / 1024
        print(f"  {p}  ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
