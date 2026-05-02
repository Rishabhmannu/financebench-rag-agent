"""Chunk documents into retrieval units with contextual prefix + structural metadata.

Chunking paths, in priority order:

1. **Docling markdown-aware** (preferred when `document["docling_doc"]` present):
   exports Docling to markdown (clean pipe tables, preserved headings), then
   chunks the markdown respecting heading boundaries and never splitting
   tables. Produces section_header, heading_path, page_number, and chunk_type
   metadata. Replaces the earlier HybridChunker path that regressed RAGAS
   scores because HybridChunker's text serializer flattened financial tables
   into noisy "label, col = value" strings that generators couldn't parse.

2. **Docling HybridChunker** (legacy path, available via `_chunk_with_docling`
   but no longer the primary — kept for reference and comparison smoke tests).

3. **Per-page recursive split** (fallback when `docling_doc` is None but `pages`
   are available — SKIP_DOCLING=1 mode, or Docling failed on a specific PDF):
   chunks each page independently, tags with page_number. Tables get flattened.

4. **Flat text recursive split** (last resort — no pages, no Docling): chunks
   `document["text"]` without page tracking.

Sprint 7a.v2 adds a **Contextual Retrieval prefix** (Anthropic pattern) to each
chunk's content before embedding. Prepending a compact metadata header like
`[Company: Apple Inc. | FY2023 10-K | Page 4]` makes dense embeddings inherently
aware of the chunk's source entity.
"""

import logging
import re

logger = logging.getLogger(__name__)

# ~512 tokens for English text (avg ~1.5 chars/token for mixed content)
MAX_CHUNK_CHARS = 800
# Markdown chunker uses a larger budget so a section's narrative + its table
# often fit in one chunk (matches HybridChunker's ~1500-char avg, which was
# actually fine — it was the TEXT serialization that regressed, not the size)
MARKDOWN_MAX_CHUNK_CHARS = 1500
OVERLAP_CHARS = 150
HYBRID_MAX_TOKENS = 512  # Docling HybridChunker target size; fits text-embedding-3-small budget

# Separators tried in order — prefer structural splits over arbitrary ones
SEPARATORS = ["\n\n", "\n", ". ", " "]

# Markdown patterns
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|")


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

    Priority: markdown-first Docling chunker → HybridChunker fallback → per-page
    pypdf fallback → flat-text split. See module docstring for the full waterfall.
    Each chunk's `content` is prefixed with a context header (company / FY / page),
    raw text preserved in `raw_content`.
    """
    # 1. Docling markdown-first (pipe tables preserved; primary path).
    docling_doc = document.get("docling_doc")
    if docling_doc is not None:
        try:
            md_chunks = _chunk_with_docling_markdown(docling_doc, metadata)
            if md_chunks:
                return md_chunks
            logger.warning(
                f"Markdown chunker returned 0 chunks on {metadata.get('source_file', '?')}; "
                f"falling back to HybridChunker."
            )
        except Exception as e:
            logger.warning(
                f"Markdown chunker failed on {metadata.get('source_file', '?')}: "
                f"{type(e).__name__}: {e}. Falling back to HybridChunker."
            )
        # 1b. HybridChunker fallback (legacy; flatter table serialization)
        try:
            return _chunk_with_docling(docling_doc, metadata)
        except Exception as e:
            logger.warning(
                f"HybridChunker also failed on {metadata.get('source_file', '?')}: "
                f"{type(e).__name__}: {e}. Falling back to per-page pypdf chunking."
            )

    # 2. Per-page pypdf fallback (SKIP_DOCLING=1 or Docling failed)
    pages = document.get("pages")
    if pages:
        return _chunk_per_page(pages, metadata)

    # 3. Flat-text last resort
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


def _chunk_with_docling(docling_doc, metadata: dict) -> list[dict]:
    """Use Docling HybridChunker for structure-aware chunking.

    Each chunk carries:
      - section_header + heading_path (derived from Docling's headings)
      - page_number (smallest page_no across chunk's doc_items)
      - chunk_type ("table" when any doc_item is a table, else "text")
    """
    from docling.chunking import HybridChunker

    chunker = HybridChunker(max_tokens=HYBRID_MAX_TOKENS, merge_peers=True)

    result = []
    for idx, chunk in enumerate(chunker.chunk(docling_doc)):
        chunk_meta = dict(metadata)  # copy base metadata
        chunk_meta["chunk_index"] = idx

        # Derive structural metadata from the chunk's DocMeta
        headings: list[str] = []
        page_numbers: set[int] = set()
        chunk_type = "text"

        meta_obj = getattr(chunk, "meta", None)
        if meta_obj is not None:
            try:
                meta_dict = meta_obj.export_json_dict() if hasattr(meta_obj, "export_json_dict") else {}
            except Exception:
                meta_dict = {}

            # Headings: Docling stores them as a list of strings in hierarchical order
            raw_headings = meta_dict.get("headings") or []
            if isinstance(raw_headings, list):
                headings = [str(h) for h in raw_headings if h]

            # doc_items: each carries a `prov` list with page numbers; and a `label`
            # indicating the item type ("table", "paragraph", "list_item", ...)
            for item in meta_dict.get("doc_items") or []:
                label = str(item.get("label", "")).lower()
                if "table" in label:
                    chunk_type = "table"
                for prov in item.get("prov") or []:
                    page_no = prov.get("page_no")
                    if isinstance(page_no, int):
                        page_numbers.add(page_no)

        if headings:
            chunk_meta["section_header"] = headings[-1]
            chunk_meta["heading_path"] = " > ".join(headings)
        page_number = min(page_numbers) if page_numbers else None
        if page_number is not None:
            chunk_meta["page_number"] = page_number
        chunk_meta["chunk_type"] = chunk_type

        # Build contextual prefix now that we know the page
        prefix = _build_context_prefix(metadata, page_number=page_number)
        chunk_text = getattr(chunk, "text", "") or ""

        result.append({
            "content": prefix + chunk_text,
            "raw_content": chunk_text,
            "metadata": chunk_meta,
        })

    return result


def _build_heading_page_map(docling_doc) -> dict[str, int]:
    """Walk DoclingDocument items, map section-heading text -> smallest page_no.

    Used by `_chunk_with_docling_markdown` to correlate markdown `#`-headings
    (which have no page info) back to PDF page numbers.
    """
    page_by_heading: dict[str, int] = {}
    try:
        iterator = docling_doc.iterate_items()
    except Exception:
        return page_by_heading

    for entry in iterator:
        # iterate_items() yields either `item` or `(item, level)` depending on version
        item = entry[0] if isinstance(entry, tuple) else entry
        label = str(getattr(item, "label", "")).lower()
        if not any(tag in label for tag in ("section_header", "heading", "title")):
            continue
        text = (getattr(item, "text", "") or "").strip()
        if not text:
            continue
        provs = getattr(item, "prov", None) or []
        for p in provs:
            pn = getattr(p, "page_no", None)
            if isinstance(pn, int):
                if text not in page_by_heading or pn < page_by_heading[text]:
                    page_by_heading[text] = pn
                break
    return page_by_heading


def _parse_markdown_blocks(md: str) -> list[dict]:
    """Split markdown into a sequence of typed blocks: heading, table, paragraph.

    A "table" block is any run of consecutive lines starting with `|`. A
    heading is a single line matching `^#{1,6} `. Paragraphs are everything
    else, grouped across non-blank lines.
    """
    lines = md.split("\n")
    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue

        m = _HEADING_RE.match(line)
        if m:
            blocks.append({"type": "heading", "level": len(m.group(1)), "text": m.group(2).strip()})
            i += 1
            continue

        if _TABLE_ROW_RE.match(line):
            start = i
            while i < len(lines) and _TABLE_ROW_RE.match(lines[i]):
                i += 1
            blocks.append({"type": "table", "text": "\n".join(lines[start:i])})
            continue

        # Paragraph: collect until blank line / heading / table
        start = i
        while i < len(lines):
            ln = lines[i]
            if not ln.strip() or _HEADING_RE.match(ln) or _TABLE_ROW_RE.match(ln):
                break
            i += 1
        para = "\n".join(lines[start:i]).strip()
        if para:
            blocks.append({"type": "paragraph", "text": para})
    return blocks


def _chunk_with_docling_markdown(docling_doc, metadata: dict) -> list[dict]:
    """Markdown-first chunker.

    Rationale: HybridChunker's `chunk.text` serializer flattens dense multi-year
    financial tables into noisy prose like "Operating income, Year Ended Dec
    31,.2017 = $ 838,679" which RAGAS-measurably hurt faithfulness/precision.
    Docling's `export_to_markdown()` instead renders tables as clean pipe
    tables that embeddings and generators can actually parse.

    Algorithm:
      1. Export full markdown; build heading->page map from doc items.
      2. Parse markdown into heading / table / paragraph blocks.
      3. Walk blocks, tracking current heading stack. Tables are emitted
         as their own chunk (never split). Paragraphs pack greedily up to
         MARKDOWN_MAX_CHUNK_CHARS; oversize paragraphs fall back to the
         recursive splitter.
      4. Each chunk carries section_header (deepest current heading),
         heading_path (full stack), page_number (from heading map), and
         chunk_type ("table" iff any member block is a table).
    """
    try:
        md = docling_doc.export_to_markdown()
    except Exception as e:
        logger.warning(f"export_to_markdown failed: {type(e).__name__}: {e}")
        return []

    if not md.strip():
        return []

    heading_page_map = _build_heading_page_map(docling_doc)
    blocks = _parse_markdown_blocks(md)

    chunks: list[dict] = []
    heading_stack: list[tuple[int, str]] = []  # [(level, text), ...]
    pending_blocks: list[dict] = []
    pending_len = 0

    def _emit() -> None:
        nonlocal pending_blocks, pending_len
        if not pending_blocks:
            return
        chunk_text = "\n\n".join(b["text"] for b in pending_blocks).strip()
        if not chunk_text:
            pending_blocks = []
            pending_len = 0
            return

        section_header = heading_stack[-1][1] if heading_stack else ""
        heading_path = " > ".join(h[1] for h in heading_stack) if heading_stack else ""
        chunk_type = "table" if any(b["type"] == "table" for b in pending_blocks) else "text"

        # Page = most recent heading in stack that we have a page for
        page_number: int | None = None
        for _lvl, htext in reversed(heading_stack):
            if htext in heading_page_map:
                page_number = heading_page_map[htext]
                break

        chunk_meta = dict(metadata)
        chunk_meta["chunk_index"] = len(chunks)
        if section_header:
            chunk_meta["section_header"] = section_header
        if heading_path:
            chunk_meta["heading_path"] = heading_path
        if page_number is not None:
            chunk_meta["page_number"] = page_number
        chunk_meta["chunk_type"] = chunk_type

        prefix = _build_context_prefix(metadata, page_number=page_number)
        chunks.append({
            "content": prefix + chunk_text,
            "raw_content": chunk_text,
            "metadata": chunk_meta,
        })
        pending_blocks = []
        pending_len = 0

    for block in blocks:
        if block["type"] == "heading":
            # Heading transitions flush the current chunk so new chunks carry
            # the new section's metadata cleanly.
            _emit()
            lvl = block["level"]
            while heading_stack and heading_stack[-1][0] >= lvl:
                heading_stack.pop()
            heading_stack.append((lvl, block["text"]))
            continue

        blen = len(block["text"])

        if block["type"] == "table":
            # Never split tables. Flush current, emit table alone, continue.
            if pending_len > 0:
                _emit()
            pending_blocks.append(block)
            pending_len += blen
            _emit()
            continue

        # Paragraph
        if blen > MARKDOWN_MAX_CHUNK_CHARS:
            # Oversize — flush, then recursive-split this paragraph.
            if pending_len > 0:
                _emit()
            for sub in _recursive_split(block["text"], MARKDOWN_MAX_CHUNK_CHARS, OVERLAP_CHARS):
                pending_blocks = [{"type": "paragraph", "text": sub}]
                pending_len = len(sub)
                _emit()
            continue

        if pending_len > 0 and pending_len + blen > MARKDOWN_MAX_CHUNK_CHARS:
            _emit()
        pending_blocks.append(block)
        pending_len += blen

    _emit()
    return chunks


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
