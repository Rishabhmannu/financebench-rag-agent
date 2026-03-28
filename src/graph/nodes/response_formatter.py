import logging

from src.models.state import RAGState

logger = logging.getLogger(__name__)


def response_formatter_node(state: RAGState) -> dict:
    """Format the final response with citations and metadata."""
    answer = state.get("generated_answer", "")
    chunks = state.get("relevant_chunks", [])
    hallucination_score = state.get("hallucination_score", 0.0)
    hallucination_status = state.get("hallucination_status", "unknown")

    # Build source list
    sources = []
    seen = set()
    for chunk in chunks:
        meta = chunk.get("metadata", {})
        source_file = meta.get("source_file", "Unknown")
        if source_file not in seen:
            seen.add(source_file)
            sources.append({
                "file": source_file,
                "page": meta.get("page_number"),
                "section": meta.get("section_header", ""),
                "doc_type": meta.get("doc_type", ""),
            })

    # Add disclaimer if hallucination check was uncertain
    if hallucination_status == "hallucinated":
        answer = (
            "**Note:** This answer could not be fully verified against source documents. "
            "Please verify the information independently.\n\n" + answer
        )

    metadata = {
        "sources": sources,
        "confidence": hallucination_score,
        "chunks_used": len(chunks),
        "hallucination_status": hallucination_status,
    }

    return {
        "final_response": answer,
        "response_metadata": metadata,
    }
