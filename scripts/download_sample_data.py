"""Download real SEC 10-K filings and generate synthetic financial PDFs.

This script populates data/sample/ with documents for all three doc_types:
  - 10k:            Real SEC 10-K filing excerpts (Apple, Microsoft, Tesla)
  - invoice:        Synthetic but realistic vendor invoices
  - expense_policy: Synthetic corporate expense policy documents

Usage:
    python scripts/download_sample_data.py
"""

import logging
import sys
import time
from pathlib import Path

import requests
from fpdf import FPDF

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SAMPLE_DIR = Path("data/sample")
SEC_HEADERS = {
    "User-Agent": "RAGAgentDemo/1.0 (rishabh@example.com)",
    "Accept-Encoding": "gzip, deflate",
}


def sanitize_for_pdf(text: str) -> str:
    """Replace Unicode characters that can't be encoded in Latin-1."""
    replacements = {
        "\u2014": "--",   # em dash
        "\u2013": "-",    # en dash
        "\u2018": "'",    # left single quote
        "\u2019": "'",    # right single quote
        "\u201c": '"',    # left double quote
        "\u201d": '"',    # right double quote
        "\u2026": "...",  # ellipsis
        "\u2022": "-",    # bullet
        "\u00a0": " ",    # non-breaking space
    }
    for char, repl in replacements.items():
        text = text.replace(char, repl)
    return text.encode("latin-1", errors="replace").decode("latin-1")

# ---------------------------------------------------------------------------
# 1. SEC 10-K Filing Downloader
# ---------------------------------------------------------------------------

# Company CIKs (Central Index Keys) for SEC EDGAR
COMPANIES = {
    "apple": {"cik": "0000320193", "name": "Apple Inc."},
    "microsoft": {"cik": "0000789019", "name": "Microsoft Corporation"},
    "tesla": {"cik": "0001318605", "name": "Tesla Inc."},
}


def fetch_sec_filing_text(cik: str, company_name: str) -> str | None:
    """Fetch the most recent 10-K filing text from SEC EDGAR."""
    # Step 1: Get the company's filing index
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    logger.info(f"Fetching filing index for {company_name} from EDGAR...")

    try:
        resp = requests.get(submissions_url, headers=SEC_HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"Failed to fetch filing index for {company_name}: {e}")
        return None

    # Step 2: Find the most recent 10-K filing
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accession_numbers = recent.get("accessionNumber", [])
    primary_docs = recent.get("primaryDocument", [])

    for i, form in enumerate(forms):
        if form == "10-K":
            accession = accession_numbers[i].replace("-", "")
            primary_doc = primary_docs[i]
            filing_url = f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/{accession}/{primary_doc}"

            logger.info(f"Downloading 10-K filing: {filing_url}")
            time.sleep(0.2)  # SEC rate limit: 10 requests/sec

            try:
                doc_resp = requests.get(filing_url, headers=SEC_HEADERS, timeout=60)
                doc_resp.raise_for_status()
                return doc_resp.text
            except Exception as e:
                logger.warning(f"Failed to download filing document: {e}")
                return None

    logger.warning(f"No 10-K filing found for {company_name}")
    return None


def extract_financial_sections(html_text: str, company_name: str) -> str:
    """Extract key text content from a 10-K HTML filing.

    We strip HTML tags and extract a meaningful excerpt (first ~3000 words)
    covering the financial data sections.
    """
    import re

    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", html_text)
    # Remove excessive whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # Remove common boilerplate
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&#\d+;", "", text)

    # Try to find the start of financial discussion
    financial_keywords = [
        "Item 6", "Item 7", "Item 8",
        "Selected Financial Data",
        "Management's Discussion",
        "Financial Statements",
        "Results of Operations",
        "Total net revenue", "Total revenue",
        "Net income", "Operating income",
    ]

    best_start = 0
    for keyword in financial_keywords:
        idx = text.lower().find(keyword.lower())
        if idx > 0 and (best_start == 0 or idx < best_start):
            best_start = max(0, idx - 200)

    # Take ~3000 words from the financial section
    excerpt = text[best_start:]
    words = excerpt.split()[:3000]
    return " ".join(words)


def create_10k_pdf(text: str, company_name: str, filename: str) -> None:
    """Convert 10-K text to a simple PDF."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # Title
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, f"{company_name}", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Annual Report (Form 10-K)", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.cell(0, 8, "Filed with the Securities and Exchange Commission", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(10)

    # Body
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(0, 4.5, sanitize_for_pdf(text))

    output_path = SAMPLE_DIR / filename
    pdf.output(str(output_path))
    logger.info(f"Created 10-K PDF: {output_path} ({output_path.stat().st_size / 1024:.0f} KB)")


# ---------------------------------------------------------------------------
# 2. Fallback: Embedded 10-K Excerpts (if SEC download fails)
# ---------------------------------------------------------------------------

FALLBACK_10K_DATA = {
    "apple": {
        "name": "Apple Inc.",
        "text": """APPLE INC.
FORM 10-K — ANNUAL REPORT
Fiscal Year Ended September 30, 2023

ITEM 6. SELECTED FINANCIAL DATA

The following selected financial data should be read in conjunction with the Consolidated Financial Statements and accompanying Notes.

(In millions, except per share amounts)

                                    2023        2022        2021        2020        2019
Total net revenue               $383,285    $394,328    $365,817    $274,515    $260,174
Cost of sales                    214,137     223,546     212,981     169,559     161,782
Gross margin                     169,148     170,782     152,836     104,956      98,392
Operating expenses                54,847      51,345      43,887      38,668      34,462
Operating income                 114,301     119,437     108,949      66,288      63,930
Net income                        96,995     99,803      94,680      57,411      55,256

Earnings per share:
  Basic                           $6.16       $6.15       $5.67       $3.31       $2.99
  Diluted                         $6.13       $6.11       $5.61       $3.28       $2.97

Total assets                    $352,583    $352,755    $351,002    $323,888    $338,516
Total long-term debt            $95,281     $98,959     $109,106    $98,667     $91,807

ITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS OF FINANCIAL CONDITION AND RESULTS OF OPERATIONS

The following discussion should be read in conjunction with the Consolidated Financial Statements and Notes. This section contains forward-looking statements that involve risks and uncertainties.

Revenue

Total net revenue decreased 3% or $11.0 billion during 2023 compared to 2022. The decrease was driven by lower iPhone and Mac revenue, partially offset by higher revenue from Services.

Products revenue decreased 5% during 2023 compared to 2022, driven by lower iPhone and Mac revenue. The weakness in Products revenue was broad-based across all geographic segments.

Services revenue increased 9% during 2023 compared to 2022, driven by growth in advertising, the App Store and cloud services. Services revenue reached $85.2 billion, an all-time high. The Company's installed base of active devices continued to grow and reached an all-time high.

iPhone revenue was $200.6 billion during 2023, a decrease of 2% compared to 2022. The decrease was driven primarily by lower unit sales in the Greater China segment and unfavorable foreign currency movements.

Mac revenue was $29.4 billion during 2023, a decrease of 27% compared to 2022. The decrease was primarily driven by lower laptop sales and the different timing of product launches.

iPad revenue was $28.3 billion during 2023, a decrease of 3% compared to 2022.

Wearables, Home and Accessories revenue was $39.8 billion during 2023, a decrease of 3% compared to 2022.

Gross Margin

Total gross margin decreased during 2023 compared to 2022 due to the decrease in total net revenue. Gross margin percentage increased to 44.1% during 2023 compared to 43.3% during 2022, driven primarily by a favorable shift in product mix toward Services and cost savings.

Products gross margin percentage was 36.5% during 2023 compared to 36.3% during 2022.
Services gross margin percentage was 70.8% during 2023 compared to 71.7% during 2022.

Operating Expenses

Total operating expenses increased 7% or $3.5 billion during 2023 compared to 2022. Research and development expense increased 14% due to increases in headcount-related expenses. Selling, general and administrative expense increased 1%.

Research and development expense was $29.9 billion during 2023 compared to $26.3 billion during 2022.

ITEM 8. FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA

CONSOLIDATED STATEMENTS OF OPERATIONS (In millions)

                                        2023         2022
Net sales:
  Products                           $298,085     $316,199
  Services                             85,200       78,129
    Total net sales                   383,285      394,328

Cost of sales:
  Products                            189,282      201,471
  Services                             24,855       22,075
    Total cost of sales               214,137      223,546

Gross margin                          169,148      170,782

Operating expenses:
  Research and development             29,915       26,251
  Selling, general and administrative  24,932       25,094
    Total operating expenses           54,847       51,345

Operating income                      114,301      119,437
Other income/(expense), net            (382)        (334)
Income before provision for taxes     113,919      119,103
Provision for income taxes             16,924       19,300
Net income                          $  96,995    $  99,803
""",
    },
    "microsoft": {
        "name": "Microsoft Corporation",
        "text": """MICROSOFT CORPORATION
FORM 10-K — ANNUAL REPORT
Fiscal Year Ended June 30, 2023

ITEM 6. SELECTED FINANCIAL DATA

(In millions, except per share amounts)

                                        2023        2022        2021        2020        2019
Revenue                             $211,915    $198,270    $168,088    $143,015    $125,843
Cost of revenue                       65,863      62,650      52,232      46,078      42,910
Gross profit                         146,052     135,620     115,856      96,937      82,933
Operating income                      88,523      83,383      69,916      52,959      42,959
Net income                            72,361      72,738      61,271      44,281      39,240

Diluted earnings per share            $ 9.68      $ 9.65      $ 8.05      $ 5.76      $ 5.06

Total assets                        $411,976    $364,840    $333,779    $301,311    $286,556
Long-term debt                      $ 41,990    $ 47,032    $ 50,074    $ 59,578    $ 66,662

ITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS

OVERVIEW

Microsoft is a technology company whose mission is to empower every person and every organization on the planet to achieve more. We generate revenue by offering a wide range of cloud-based solutions, content, and other services to people and businesses; licensing and supporting an array of software products; and delivering relevant online advertising to a global audience.

Revenue increased $13.6 billion or 7% driven by growth across each of our segments. Intelligent Cloud revenue increased driven by Azure and other cloud services. Productivity and Business Processes revenue increased driven by Office 365 Commercial. More Personal Computing revenue increased driven by search and news advertising.

SEGMENT RESULTS OF OPERATIONS

Productivity and Business Processes
Revenue increased $3.5 billion or 9%.
  - Office Commercial products and cloud services revenue increased 10%, driven by Office 365 Commercial revenue growth of 13%.
  - Office Consumer products and cloud services revenue increased 2%.
  - LinkedIn revenue increased 10%.
  - Dynamics products and cloud services revenue increased 16%, driven by Dynamics 365 revenue growth of 24%.

Intelligent Cloud
Revenue increased $7.0 billion or 11%.
  - Server products and cloud services revenue increased 17%, driven by Azure and other cloud services revenue growth of 29%.
  - Azure revenue growth was driven by Azure consumption-based services.

More Personal Computing
Revenue increased $3.1 billion or 4%.
  - Windows OEM revenue decreased 3%.
  - Windows Commercial products and cloud services revenue increased 5%.
  - Search and news advertising revenue excluding traffic acquisition costs increased 23%.
  - Xbox content and services revenue decreased 1%.
  - Surface revenue decreased 20%.
  - Gaming revenue increased 1%.

OPERATING EXPENSES
Research and development expenses increased $1.1 billion or 4% driven by investments in cloud engineering and LinkedIn. Sales and marketing expenses increased $0.5 billion or 2%. General and administrative expenses increased $0.2 billion or 3%.

LIQUIDITY AND CAPITAL RESOURCES
As of June 30, 2023, we had $111.3 billion in cash, cash equivalents, and short-term investments. Cash from operations was $87.6 billion, an increase of $2.2 billion from the prior year.
""",
    },
    "tesla": {
        "name": "Tesla Inc.",
        "text": """TESLA, INC.
FORM 10-K — ANNUAL REPORT
Fiscal Year Ended December 31, 2023

ITEM 6. SELECTED FINANCIAL DATA

(In millions, except per share amounts)

                                        2023        2022        2021        2020        2019
Total revenues                      $ 96,773    $ 81,462    $ 53,823    $ 31,536    $ 24,578
Cost of revenues                      79,113      60,609      40,217      24,906      20,509
Gross profit                          17,660      20,853      13,606       6,630       4,069
Operating income                       8,891      13,656       6,523       1,994         (69)
Net income attributable to common
  stockholders                         7,928      12,556       5,519         721         (862)

Diluted earnings per share             $2.49       $3.62       $1.63       $0.21      ($0.33)

Total assets                        $106,618    $ 82,338    $ 62,131    $ 52,148    $ 34,309
Total long-term debt and finance
  leases, net of current portion     $ 2,857    $  1,597    $  5,245    $  9,607    $ 11,634

ITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS

OVERVIEW

We design, develop, manufacture, sell and lease high-performance fully electric vehicles and energy generation and storage systems, and offer services related to our products. We are the world's most valuable automotive company by market capitalization.

Revenue for the year ended December 31, 2023 increased by $15.31 billion, or 19%, compared to the prior year. The increase was primarily due to growth in vehicle deliveries, partially offset by a decrease in average selling price.

REVENUE
Automotive revenue increased by $11.57 billion or 15% year-over-year. This was primarily driven by an increase of 38% in vehicle deliveries (1,808,581 vehicles in 2023 vs 1,313,851 in 2022). Average selling price per vehicle decreased approximately 17% year-over-year due to price reductions across our vehicle lineup to stimulate demand.

Energy generation and storage revenue increased by $4.03 billion or 54%, driven by deployments of Megapack and Powerwall products to utility and commercial customers.

Services and other revenue increased by $1.60 billion or 27% year-over-year, driven by growth in our Supercharger network, body shop and parts, used vehicle sales, and insurance services.

COST OF REVENUES AND GROSS MARGIN
Total cost of revenues increased by $18.50 billion, or 31%, primarily due to higher raw material costs and manufacturing costs associated with increased production volume. Total gross margin was 18.2% compared to 25.6% in the prior year. The decline was driven by price reductions, higher raw material costs, and increased costs associated with new factory ramp-ups.

Automotive gross margin decreased from 28.5% to 18.2%, primarily driven by vehicle price reductions globally. We believe these price adjustments are necessary to maintain demand growth and market share.

OPERATING EXPENSES
Research and development expenses increased $0.93 billion or 26% to $4.46 billion, driven by investments in AI training compute for Full Self-Driving, Cybertruck development, and next-generation vehicle platform development.

Selling, general and administrative expenses increased $0.60 billion or 13% to $5.22 billion, driven by growth in our global operations and workforce.

LIQUIDITY AND CAPITAL RESOURCES
As of December 31, 2023, we had $29.1 billion in cash and cash equivalents and investments. Operating cash flow was $13.3 billion.
""",
    },
}


# ---------------------------------------------------------------------------
# 3. Synthetic Invoice Generator
# ---------------------------------------------------------------------------

INVOICES = [
    {
        "filename": "invoice_techsolutions_001.pdf",
        "vendor": "TechSolutions Corp.",
        "vendor_address": "1234 Innovation Drive, Suite 500\nSan Jose, CA 95134",
        "invoice_no": "TS-2024-0847",
        "date": "January 15, 2024",
        "due_date": "February 14, 2024",
        "bill_to": "Acme Financial Group\n500 Market Street, Floor 20\nSan Francisco, CA 94105",
        "items": [
            ("Cloud Infrastructure Services - Q1 2024", 1, 45000.00),
            ("Data Analytics Platform License (Annual)", 1, 28500.00),
            ("Professional Services - System Integration", 120, 225.00),
            ("24/7 Premium Support Package", 1, 12000.00),
        ],
        "notes": "Payment terms: Net 30. Late payments subject to 1.5% monthly interest.",
        "confidentiality": "internal",
    },
    {
        "filename": "invoice_globalconsulting_002.pdf",
        "vendor": "Global Consulting Partners LLP",
        "vendor_address": "200 Park Avenue, 35th Floor\nNew York, NY 10166",
        "invoice_no": "GCP-2024-3291",
        "date": "February 28, 2024",
        "due_date": "March 29, 2024",
        "bill_to": "Acme Financial Group\n500 Market Street, Floor 20\nSan Francisco, CA 94105",
        "items": [
            ("Strategic Advisory Services - M&A Due Diligence", 240, 450.00),
            ("Market Analysis Report - Fintech Sector", 1, 35000.00),
            ("Regulatory Compliance Assessment", 80, 375.00),
            ("Travel and Expenses (pre-approved)", 1, 8750.00),
        ],
        "notes": "This invoice relates to the Project Phoenix engagement (SOW #GCP-2023-089). "
        "All services rendered per the Master Services Agreement dated June 1, 2023.",
        "confidentiality": "confidential",
    },
    {
        "filename": "invoice_cloudservices_003.pdf",
        "vendor": "Pinnacle Cloud Services Inc.",
        "vendor_address": "8800 Technology Blvd\nSeattle, WA 98109",
        "invoice_no": "PCS-INV-2024-0156",
        "date": "March 10, 2024",
        "due_date": "April 9, 2024",
        "bill_to": "Acme Financial Group\n500 Market Street, Floor 20\nSan Francisco, CA 94105",
        "items": [
            ("AWS Reserved Instances - Production (Monthly)", 12, 18500.00),
            ("Kubernetes Cluster Management", 1, 7500.00),
            ("Data Transfer Fees (Outbound: 45 TB)", 45, 90.00),
            ("Security Monitoring & Incident Response", 1, 5200.00),
            ("Disaster Recovery Service (Hot Standby)", 1, 3800.00),
        ],
        "notes": "Usage period: February 1 - February 29, 2024. Detailed usage breakdown "
        "available upon request. Service credit of $2,100 applied for February 12 outage per SLA.",
        "confidentiality": "internal",
    },
]


def create_invoice_pdf(invoice: dict) -> None:
    """Generate a realistic-looking invoice PDF."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # Header
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 12, "INVOICE", new_x="LMARGIN", new_y="NEXT", align="R")
    pdf.ln(2)

    # Vendor info
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 6, invoice["vendor"], new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    for line in invoice["vendor_address"].split("\n"):
        pdf.cell(0, 4.5, line, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    # Invoice details
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(95, 6, f"Bill To:", new_x="RIGHT", new_y="TOP")
    pdf.cell(95, 6, f"Invoice Details:", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "", 9)
    bill_lines = invoice["bill_to"].split("\n")
    details = [
        f"Invoice #: {invoice['invoice_no']}",
        f"Date: {invoice['date']}",
        f"Due Date: {invoice['due_date']}",
    ]
    max_lines = max(len(bill_lines), len(details))
    for i in range(max_lines):
        left = bill_lines[i] if i < len(bill_lines) else ""
        right = details[i] if i < len(details) else ""
        pdf.cell(95, 4.5, left, new_x="RIGHT", new_y="TOP")
        pdf.cell(95, 4.5, right, new_x="LMARGIN", new_y="NEXT")

    pdf.ln(8)

    # Items table header
    pdf.set_fill_color(40, 60, 100)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(90, 7, " Description", fill=True, new_x="RIGHT", new_y="TOP")
    pdf.cell(25, 7, "Qty", fill=True, align="C", new_x="RIGHT", new_y="TOP")
    pdf.cell(35, 7, "Unit Price", fill=True, align="R", new_x="RIGHT", new_y="TOP")
    pdf.cell(40, 7, "Amount", fill=True, align="R", new_x="LMARGIN", new_y="NEXT")

    # Items
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 9)
    total = 0.0
    for i, (desc, qty, price) in enumerate(invoice["items"]):
        amount = qty * price
        total += amount
        bg = (245, 245, 250) if i % 2 == 0 else (255, 255, 255)
        pdf.set_fill_color(*bg)
        pdf.cell(90, 6, f" {desc}", fill=True, new_x="RIGHT", new_y="TOP")
        pdf.cell(25, 6, str(qty), fill=True, align="C", new_x="RIGHT", new_y="TOP")
        pdf.cell(35, 6, f"${price:,.2f}", fill=True, align="R", new_x="RIGHT", new_y="TOP")
        pdf.cell(40, 6, f"${amount:,.2f}", fill=True, align="R", new_x="LMARGIN", new_y="NEXT")

    # Totals
    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(150, 7, "Subtotal:", align="R", new_x="RIGHT", new_y="TOP")
    pdf.cell(40, 7, f"${total:,.2f}", align="R", new_x="LMARGIN", new_y="NEXT")

    tax = total * 0.0875  # CA sales tax
    pdf.cell(150, 7, "Tax (8.75%):", align="R", new_x="RIGHT", new_y="TOP")
    pdf.cell(40, 7, f"${tax:,.2f}", align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "B", 12)
    grand_total = total + tax
    pdf.cell(150, 8, "TOTAL DUE:", align="R", new_x="RIGHT", new_y="TOP")
    pdf.cell(40, 8, f"${grand_total:,.2f}", align="R", new_x="LMARGIN", new_y="NEXT")

    # Notes
    pdf.ln(10)
    pdf.set_font("Helvetica", "I", 8)
    pdf.multi_cell(0, 4, sanitize_for_pdf(f"Notes: {invoice['notes']}"))

    # Confidentiality footer
    pdf.ln(5)
    pdf.set_font("Helvetica", "B", 7)
    conf = invoice["confidentiality"].upper()
    pdf.cell(0, 4, sanitize_for_pdf(f"CONFIDENTIALITY: {conf} -- This document is for authorized recipients only."), align="C")

    output_path = SAMPLE_DIR / invoice["filename"]
    pdf.output(str(output_path))
    logger.info(f"Created invoice PDF: {output_path}")


# ---------------------------------------------------------------------------
# 4. Synthetic Expense Policy Generator
# ---------------------------------------------------------------------------

EXPENSE_POLICIES = [
    {
        "filename": "expense_policy_corporate_travel.pdf",
        "title": "Corporate Travel & Expense Policy",
        "version": "v3.2",
        "effective_date": "January 1, 2024",
        "sections": [
            ("1. PURPOSE AND SCOPE", """This policy establishes the guidelines and procedures for business travel and related expense reimbursement for all employees of Acme Financial Group ("the Company"). This policy applies to all full-time, part-time, and contract employees who incur expenses while conducting Company business.

The objective of this policy is to ensure that employees are reimbursed fairly for legitimate business expenses while maintaining fiscal responsibility and compliance with applicable tax regulations."""),

            ("2. APPROVAL REQUIREMENTS", """All business travel must be pre-approved by the employee's direct manager. Additional approval levels are required based on estimated total trip cost:

- Up to $5,000: Direct manager approval
- $5,001 - $25,000: Director-level approval
- $25,001 - $100,000: VP-level approval
- Over $100,000: C-suite approval required

International travel requires additional approval from the Legal and Compliance department regardless of cost.

All approvals must be obtained via the Company's expense management system (Concur) prior to booking travel. Retroactive approvals are discouraged and may result in delayed reimbursement."""),

            ("3. AIR TRAVEL", """Economy class is the standard for all domestic flights under 6 hours. Business class may be booked for:
- International flights exceeding 6 hours
- Domestic flights when economy class is unavailable within the Company's preferred fare guidelines
- Travel by VP-level and above employees (at their discretion)
- Medical accommodation with HR approval

First class travel is not reimbursable except with written C-suite pre-approval for extraordinary circumstances.

Employees must book through the Company's designated travel agency (Corporate Travel International) or the approved booking platform. Personal loyalty program preferences should not result in higher fares. The maximum reimbursable airfare for domestic travel is $1,500 round-trip; exceptions require director approval."""),

            ("4. LODGING", """Hotel accommodations should be reasonable and appropriate for the business destination. Guidelines:

- Domestic: Maximum $250/night (standard markets), $350/night (high-cost cities: NYC, SF, LA, Boston, DC)
- International: Maximum $300/night (standard), $450/night (major financial centers: London, Tokyo, Hong Kong, Singapore)

Employees are encouraged to use the Company's preferred hotel partners to leverage negotiated corporate rates. Extended stays (>5 nights) should consider apartment-style accommodations for cost savings.

Room service, minibar charges, and in-room entertainment are not reimbursable. Laundry service is reimbursable for trips exceeding 5 days."""),

            ("5. MEALS AND ENTERTAINMENT", """Daily meal allowances (per diem) while traveling:

- Breakfast: $25
- Lunch: $35
- Dinner: $75
- Total daily maximum: $135

Client entertainment expenses must have a documented business purpose and attendee list. Maximum per-person limits:
- Business lunch: $75/person
- Business dinner: $150/person
- Group events (>10 people): Requires VP pre-approval

Alcohol is limited to 2 drinks per person for client entertainment and is not reimbursable for employee-only meals. Excessive entertainment expenses will be flagged for review."""),

            ("6. GROUND TRANSPORTATION", """Rental cars should be mid-size or smaller unless justified by passenger count or equipment needs. GPS and toll transponders are reimbursable; vehicle upgrades are not.

Ride-sharing (Uber, Lyft) and taxis are reimbursable for business travel. Receipts required for all rides over $25.

Personal vehicle mileage: Reimbursed at the current IRS standard rate ($0.67/mile for 2024). Employees must submit mileage logs through the expense system.

Parking and tolls are reimbursable with receipts."""),

            ("7. EXPENSE REPORTING AND REIMBURSEMENT", """All expense reports must be submitted within 30 days of the expense or return from travel, whichever is later. Reports submitted after 60 days may not be reimbursed.

Required documentation:
- Itemized receipts for all expenses over $25
- Business purpose statement for each expense
- Client/attendee names for entertainment expenses
- Manager approval in the expense management system

Reimbursement is processed within 10 business days of approved report submission and paid via direct deposit.

The Company reserves the right to audit any expense report and may request additional documentation. Fraudulent expense claims will result in disciplinary action up to and including termination and legal prosecution."""),

            ("8. NON-REIMBURSABLE EXPENSES", """The following expenses are NOT reimbursable:
- Personal entertainment, sightseeing, or recreational activities
- Traffic violations, parking tickets, or towing charges
- Personal items (clothing, toiletries, luggage)
- Spouse or companion travel expenses
- Membership fees for airline clubs or hotel loyalty programs
- Pet care or boarding fees
- Home internet or phone charges (unless pre-approved for remote work)
- Gift cards or cash equivalents
- Charitable donations (must go through Corporate Social Responsibility)
- Expenses without valid receipts (over $25 threshold)"""),
        ],
        "confidentiality": "internal",
    },
    {
        "filename": "expense_policy_procurement.pdf",
        "title": "Procurement & Vendor Payment Policy",
        "version": "v2.1",
        "effective_date": "March 1, 2024",
        "sections": [
            ("1. PURPOSE", """This policy governs the procurement of goods and services and the processing of vendor payments for Acme Financial Group. It establishes authorization levels, competitive bidding requirements, and payment processing procedures to ensure fiscal responsibility and regulatory compliance."""),

            ("2. PURCHASE AUTHORIZATION LEVELS", """All purchases must be approved based on the total commitment value:

Tier 1 ($0 - $5,000): Department Manager authorization
Tier 2 ($5,001 - $25,000): Director authorization + one competitive quote
Tier 3 ($25,001 - $100,000): VP authorization + three competitive quotes
Tier 4 ($100,001 - $500,000): SVP authorization + formal RFP process
Tier 5 (Over $500,000): CFO and CEO authorization + Board notification + formal RFP

Software and technology purchases exceeding $10,000 require additional CTO/CIO approval regardless of tier. All recurring commitments (subscriptions, leases) require approval based on the total contract value over the full term, not monthly/annual amounts.

Emergency purchases exceeding authorization limits may be made with verbal approval from the appropriate authority, but written approval must be documented within 48 hours."""),

            ("3. VENDOR ONBOARDING AND DUE DILIGENCE", """New vendors must complete the Vendor Registration Form and provide:
- W-9 (domestic) or W-8BEN (international) tax form
- Certificate of Insurance (minimum $1M general liability)
- Banking information for ACH payments
- Two business references

For vendors providing services exceeding $100,000 annually:
- Financial stability assessment (D&B or equivalent)
- Information security assessment (SOC 2 Type II preferred)
- Background check for key personnel
- Compliance with Company's Third-Party Code of Conduct

Financial services vendors must also demonstrate compliance with applicable regulations (SOX, GLBA, CCPA/GDPR as applicable)."""),

            ("4. PAYMENT TERMS AND PROCESSING", """Standard payment terms: Net 30 from invoice receipt date.

Preferred payment methods (in order):
1. ACH/Electronic Funds Transfer (standard for domestic payments)
2. Wire Transfer (international payments and urgent domestic payments >$50,000)
3. Corporate Credit Card (purchases under $5,000)
4. Check (only when electronic payment is not accepted)

Early payment discounts (e.g., 2/10 Net 30) should be taken when the discount exceeds the Company's cost of capital. The Treasury team evaluates early payment opportunities monthly.

Invoice processing:
- All invoices must reference a valid Purchase Order number
- Invoices without PO references will be returned to the vendor
- Three-way match (PO, receipt, invoice) required for goods
- Two-way match (PO, invoice) acceptable for services with manager attestation
- Disputed invoices must be flagged within 15 days of receipt"""),

            ("5. CAPITAL EXPENDITURES", """Capital expenditures (assets with useful life >1 year and cost >$5,000) require:
- Capital Expenditure Request Form (CERF) approval
- Inclusion in annual capital budget (or supplemental budget request)
- ROI analysis for expenditures exceeding $50,000
- Post-implementation review for expenditures exceeding $250,000

All capital assets must be tagged and tracked in the Company's fixed asset management system. Depreciation methods follow GAAP standards as specified by the Finance department."""),

            ("6. PROHIBITED TRANSACTIONS", """The following are strictly prohibited:
- Splitting purchases to circumvent authorization thresholds
- Personal purchases using Company funds or accounts
- Payments to entities owned by Company employees without Compliance approval
- Cash payments exceeding $100
- Prepayment for goods/services not yet delivered (without VP approval)
- Commitment to multi-year contracts without Legal review
- Payments to sanctioned entities or individuals (OFAC compliance required)"""),
        ],
        "confidentiality": "internal",
    },
]


def create_expense_policy_pdf(policy: dict) -> None:
    """Generate a corporate policy PDF."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # Header bar
    pdf.set_fill_color(25, 50, 85)
    pdf.rect(0, 0, 210, 35, "F")

    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 18)
    pdf.set_y(8)
    pdf.cell(0, 10, "ACME FINANCIAL GROUP", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, policy["title"], new_x="LMARGIN", new_y="NEXT", align="C")

    pdf.set_text_color(0, 0, 0)
    pdf.ln(10)

    # Metadata
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, sanitize_for_pdf(f"Document Version: {policy['version']}    |    Effective Date: {policy['effective_date']}    |    Classification: {policy['confidentiality'].upper()}"), new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(2)
    pdf.set_draw_color(200, 200, 200)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    # Sections
    for section_title, section_body in policy["sections"]:
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(25, 50, 85)
        pdf.cell(0, 7, section_title, new_x="LMARGIN", new_y="NEXT")

        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(30, 30, 30)
        pdf.multi_cell(0, 4.5, sanitize_for_pdf(section_body))
        pdf.ln(4)

    # Footer
    pdf.ln(5)
    pdf.set_draw_color(200, 200, 200)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(3)
    pdf.set_font("Helvetica", "I", 7)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 4, "This policy is the property of Acme Financial Group and is intended for internal use only.", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.cell(0, 4, "Unauthorized distribution is prohibited. For questions, contact the Finance Department.", align="C")

    output_path = SAMPLE_DIR / policy["filename"]
    pdf.output(str(output_path))
    logger.info(f"Created policy PDF: {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    SAMPLE_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("  Enterprise RAG Agent — Sample Data Generator")
    print("=" * 60)
    print()

    # --- 10-K Filings ---
    print("[1/3] Downloading SEC 10-K filings...")
    for key, company in COMPANIES.items():
        cik = company["cik"]
        name = company["name"]
        filename = f"10k_{key}_2023.pdf"

        # Try live SEC EDGAR download first
        html_text = fetch_sec_filing_text(cik, name)

        if html_text and len(html_text) > 1000:
            excerpt = extract_financial_sections(html_text, name)
            if len(excerpt) > 500:
                create_10k_pdf(excerpt, name, filename)
                continue

        # Fallback to embedded data
        logger.info(f"Using embedded 10-K data for {name}")
        fallback = FALLBACK_10K_DATA[key]
        create_10k_pdf(fallback["text"], fallback["name"], filename)

    # --- Invoices ---
    print("\n[2/3] Generating synthetic invoices...")
    for inv in INVOICES:
        create_invoice_pdf(inv)

    # --- Expense Policies ---
    print("\n[3/3] Generating expense policy documents...")
    for policy in EXPENSE_POLICIES:
        create_expense_policy_pdf(policy)

    # Summary
    print()
    print("=" * 60)
    files = list(SAMPLE_DIR.glob("*.pdf"))
    print(f"  Done! Generated {len(files)} PDF documents in {SAMPLE_DIR}/")
    print()
    for f in sorted(files):
        size_kb = f.stat().st_size / 1024
        doc_type = "10k" if "10k_" in f.name else "invoice" if "invoice_" in f.name else "expense_policy"
        print(f"    [{doc_type:15s}] {f.name} ({size_kb:.0f} KB)")
    print("=" * 60)


if __name__ == "__main__":
    main()
