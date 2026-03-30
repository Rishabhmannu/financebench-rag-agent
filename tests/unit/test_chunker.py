"""Unit tests for src/ingestion/chunker.py."""

from src.ingestion.chunker import MAX_CHUNK_CHARS, OVERLAP_CHARS, _recursive_split, chunk_document


# ---------------------------------------------------------------------------
# chunk_document tests
# ---------------------------------------------------------------------------

def test_chunk_document_empty_text_returns_empty_list():
    """chunk_document returns [] when the document text is empty."""
    result = chunk_document({"text": ""}, {"doc_type": "10k"})
    assert result == []


def test_chunk_document_whitespace_only_returns_empty_list():
    """chunk_document returns [] when the document text is only whitespace."""
    result = chunk_document({"text": "   \n\n  \t  "}, {"doc_type": "10k"})
    assert result == []


def test_chunk_document_missing_text_key_returns_empty_list():
    """chunk_document returns [] when the 'text' key is absent entirely."""
    result = chunk_document({}, {"doc_type": "10k"})
    assert result == []


def test_chunk_document_short_text_returns_one_chunk():
    """A short document that fits within MAX_CHUNK_CHARS produces exactly one chunk."""
    doc = {"text": "This is a short document about quarterly earnings."}
    metadata = {"doc_type": "10k", "company": "Apple Inc."}
    result = chunk_document(doc, metadata)
    assert len(result) == 1
    assert result[0]["content"] == doc["text"]


def test_chunk_document_preserves_metadata():
    """All supplied metadata keys are present in every chunk's metadata dict."""
    metadata = {
        "doc_type": "invoice",
        "company": "Tesla Inc.",
        "confidentiality": "internal",
        "source_file": "tesla_invoice_q1.pdf",
    }
    doc = {"text": "Invoice line item 1.\n\nInvoice line item 2."}
    result = chunk_document(doc, metadata)
    assert len(result) >= 1
    for chunk in result:
        for key in ("doc_type", "company", "confidentiality", "source_file"):
            assert key in chunk["metadata"], f"Missing key {key!r} in chunk metadata"
            assert chunk["metadata"][key] == metadata[key]


def test_chunk_document_adds_chunk_index_to_each_chunk():
    """Each chunk's metadata includes a sequential chunk_index starting at 0."""
    paragraph = "A" * 400
    long_text = (paragraph + "\n\n") * 6
    doc = {"text": long_text}
    result = chunk_document(doc, {"doc_type": "10k"})
    assert len(result) >= 2, "Expected at least 2 chunks from the long text"
    for i, chunk in enumerate(result):
        assert chunk["metadata"]["chunk_index"] == i


def test_chunk_document_does_not_mutate_input_metadata():
    """The original metadata dict passed in is not mutated by chunk_document."""
    metadata = {"doc_type": "10k", "company": "Apple Inc."}
    original_keys = set(metadata.keys())
    chunk_document({"text": "Some text."}, metadata)
    assert set(metadata.keys()) == original_keys, "Input metadata dict was mutated"


# ---------------------------------------------------------------------------
# _recursive_split tests
# ---------------------------------------------------------------------------

def test_recursive_split_short_text_returns_one_chunk():
    """Text shorter than chunk_size is returned as a single chunk."""
    text = "Short paragraph one.\n\nShort paragraph two."
    result = _recursive_split(text, chunk_size=800, overlap=0)
    assert len(result) == 1
    assert "Short paragraph one." in result[0]
    assert "Short paragraph two." in result[0]


def test_recursive_split_long_text_produces_multiple_chunks():
    """Text significantly longer than chunk_size produces more than one chunk."""
    paragraphs = ["X" * 300 for _ in range(6)]
    text = "\n\n".join(paragraphs)
    result = _recursive_split(text, chunk_size=800, overlap=0)
    assert len(result) >= 2, f"Expected >=2 chunks, got {len(result)}"


def test_recursive_split_falls_back_to_line_separator():
    """When text has no \\n\\n breaks, falls back to \\n splitting."""
    lines = ["Line " + str(i) + " " + "W" * 100 for i in range(20)]
    text = "\n".join(lines)
    result = _recursive_split(text, chunk_size=400, overlap=0)
    assert len(result) >= 2, "Expected multiple chunks from line-based splitting"
    for chunk in result:
        assert len(chunk) <= 500  # Allow some tolerance for overlap


def test_recursive_split_falls_back_to_sentence_separator():
    """When text has no newlines, falls back to sentence splitting."""
    sentences = ["Sentence number " + str(i) + " with some extra content." for i in range(30)]
    text = " ".join(sentences)  # No newlines at all
    result = _recursive_split(text, chunk_size=200, overlap=0)
    assert len(result) >= 2, "Expected multiple chunks from sentence splitting"


def test_recursive_split_handles_dense_text_without_separators():
    """Very long text with no separators gets hard-split."""
    text = "A" * 2000  # No spaces, no newlines
    result = _recursive_split(text, chunk_size=800, overlap=0)
    assert len(result) >= 2
    # All content should be preserved
    total_chars = sum(len(c) for c in result)
    assert total_chars >= 2000


def test_recursive_split_empty_paragraphs_are_skipped():
    """Empty paragraphs (multiple consecutive newlines) are ignored."""
    text = "First.\n\n\n\n\n\nSecond.\n\n\n\nThird."
    result = _recursive_split(text, chunk_size=800, overlap=0)
    assert len(result) == 1
    assert "First." in result[0]
    assert "Second." in result[0]
    assert "Third." in result[0]


def test_recursive_split_uses_default_params():
    """Calling _recursive_split without explicit params uses module defaults."""
    text = "Hello world."
    result = _recursive_split(text)
    assert len(result) == 1
    assert result[0] == "Hello world."


def test_recursive_split_all_content_preserved():
    """All meaningful content from input appears in at least one output chunk."""
    para_a = "Alpha paragraph content here."
    para_b = "Beta paragraph content here."
    big_para = "C" * 500
    text = f"{para_a}\n\n{big_para}\n\n{para_b}"
    result = _recursive_split(text, chunk_size=600, overlap=0)
    full_text = " ".join(result)
    assert para_a in full_text
    assert para_b in full_text
    assert big_para in full_text
