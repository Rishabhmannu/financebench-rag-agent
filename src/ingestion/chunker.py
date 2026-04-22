"""Chunk documents using recursive character text splitter.

Sprint 7a.v2 adds a **Contextual Retrieval prefix** (Anthropic pattern) to each
chunk's content before embedding. Prepending a compact metadata header like
`[Company: Apple Inc. | FY2023 10-K | Page 4]` makes dense embeddings inherently
aware of the chunk's source entity, so semantic search naturally biases toward
the correct company even when the entity extractor fails to produce a hard
filter (e.g. pronoun queries or generic prompts).
"""

import logging

logger = logging.getLogger(__name__)

# ~512 tokens for English text (avg ~1.5 chars/token for mixed content)
MAX_CHUNK_CHARS = 800
OVERLAP_CHARS = 150

# Separators tried in order — prefer structural splits over arbitrary ones
SEPARATORS = ["\n\n", "\n", ". ", " "]


def _build_context_prefix(metadata: dict, page_number: int | None = None) -> str:
    """Build a compact header like '[Company: Apple Inc. | FY2023 10-K | Page 4] '.

    Kept short (~60–100 chars) so it doesn't dominate chunk embeddings. Only
    includes fields that exist and aren't 'unknown'.
    """
    parts = []

    name = metadata.get("company_name")
    if name and name != "Unknown":
        parts.append(f"Company: {name}")

    # Fiscal year appears in source_file for our data (e.g. "10k_aapl_2023.pdf"); we
    # don't have a first-class fiscal_year metadata field yet, so derive if possible.
    source = str(metadata.get("source_file", ""))
    fy_part = ""
    for y in ("2021", "2022", "2023", "2024", "2025"):
        if y in source:
            fy_part = f"FY{y}"
            break

    doc_type = metadata.get("doc_type")
    if doc_type and doc_type != "unknown":
        doc_label = doc_type.upper() if doc_type == "10k" else doc_type.replace("_", " ").title()
        parts.append(f"{fy_part} {doc_label}".strip() if fy_part else doc_label)
    elif fy_part:
        parts.append(fy_part)

    if page_number is not None:
        parts.append(f"Page {page_number}")

    if not parts:
        return ""
    return f"[{' | '.join(parts)}] "


def chunk_document(document: dict, metadata: dict) -> list[dict]:
    """Chunk a document into pieces with metadata attached to each chunk.

    If the document includes a `pages` list (per-page text from the loader), each
    page is chunked independently and chunks are tagged with `page_number`.
    Otherwise falls back to chunking the flat `text` field (no page numbers).

    Each chunk's `content` is prefixed with a compact context header (see
    `_build_context_prefix`) so dense embeddings carry company/year/section
    signal. The raw, unprefixed chunk text is preserved as `raw_content` for
    downstream use (citations, generator context display).
    """
    pages = document.get("pages")
    if pages:
        return _chunk_per_page(pages, metadata)

    text = document.get("text", "")
    if not text.strip():
        return []

    chunks = _recursive_split(text, chunk_size=MAX_CHUNK_CHARS, overlap=OVERLAP_CHARS)
    prefix = _build_context_prefix(metadata)

    result = []
    for i, chunk_text in enumerate(chunks):
        result.append({
            "content": prefix + chunk_text,
            "raw_content": chunk_text,
            "metadata": {
                **metadata,
                "chunk_index": i,
            },
        })

    return result


def _chunk_per_page(pages: list[dict], metadata: dict) -> list[dict]:
    """Chunk each page independently, attaching page_number to every chunk."""
    result = []
    chunk_index = 0
    for page in pages:
        page_text = (page.get("text") or "").strip()
        if not page_text:
            continue
        page_number = page.get("page_number")
        prefix = _build_context_prefix(metadata, page_number=page_number)
        page_chunks = _recursive_split(page_text, chunk_size=MAX_CHUNK_CHARS, overlap=OVERLAP_CHARS)
        for chunk_text in page_chunks:
            result.append({
                "content": prefix + chunk_text,
                "raw_content": chunk_text,
                "metadata": {
                    **metadata,
                    "chunk_index": chunk_index,
                    "page_number": page_number,
                },
            })
            chunk_index += 1
    return result


def _recursive_split(
    text: str,
    chunk_size: int = 800,
    overlap: int = 150,
    separators: list[str] | None = None,
) -> list[str]:
    """Recursively split text using multiple separators, falling back as needed.

    Tries separators in order: paragraphs → lines → sentences → words.
    If a piece is still too long after splitting on the current separator,
    recurse with the next separator.
    """
    if separators is None:
        separators = SEPARATORS

    text = text.strip()
    if not text:
        return []

    # Base case: text fits in one chunk
    if len(text) <= chunk_size:
        return [text]

    # Find the best separator that actually splits the text
    separator = ""
    for sep in separators:
        if sep in text:
            separator = sep
            break

    # If no separator works, hard-split by characters
    if not separator:
        return _hard_split(text, chunk_size, overlap)

    # Split on the chosen separator
    parts = text.split(separator)
    remaining_separators = separators[separators.index(separator) + 1 :]

    # Merge small parts together, respecting chunk_size
    chunks = []
    current = ""

    for part in parts:
        part = part.strip()
        if not part:
            continue

        candidate = current + separator + part if current else part

        if len(candidate) <= chunk_size:
            current = candidate
        else:
            # Current chunk is full
            if current:
                chunks.append(current.strip())

            # If this single part exceeds chunk_size, recurse with next separator
            if len(part) > chunk_size and remaining_separators:
                sub_chunks = _recursive_split(part, chunk_size, overlap, remaining_separators)
                chunks.extend(sub_chunks)
                current = ""
            else:
                current = part

    if current.strip():
        chunks.append(current.strip())

    # Apply overlap between chunks
    if overlap > 0 and len(chunks) > 1:
        chunks = _apply_overlap(chunks, overlap)

    return chunks


def _hard_split(text: str, chunk_size: int, overlap: int) -> list[str]:
    """Last resort: split text at exact character boundaries."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end].strip())
        start = end - overlap if overlap else end
    return [c for c in chunks if c]


def _apply_overlap(chunks: list[str], overlap: int) -> list[str]:
    """Add overlap from the end of each chunk to the start of the next."""
    result = [chunks[0]]
    for i in range(1, len(chunks)):
        prev_tail = chunks[i - 1][-overlap:]
        result.append(prev_tail + " " + chunks[i])
    return result
