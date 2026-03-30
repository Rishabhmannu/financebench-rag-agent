"""Chunk documents using recursive character text splitter."""

import logging

logger = logging.getLogger(__name__)

# ~512 tokens for English text (avg ~1.5 chars/token for mixed content)
MAX_CHUNK_CHARS = 800
OVERLAP_CHARS = 150

# Separators tried in order — prefer structural splits over arbitrary ones
SEPARATORS = ["\n\n", "\n", ". ", " "]


def chunk_document(document: dict, metadata: dict) -> list[dict]:
    """Chunk a document into pieces with metadata attached to each chunk."""
    text = document.get("text", "")
    if not text.strip():
        return []

    chunks = _recursive_split(text, chunk_size=MAX_CHUNK_CHARS, overlap=OVERLAP_CHARS)

    result = []
    for i, chunk_text in enumerate(chunks):
        result.append({
            "content": chunk_text,
            "metadata": {
                **metadata,
                "chunk_index": i,
            },
        })

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
