import logging

from langchain_core.messages import HumanMessage, SystemMessage

from src.config.prompts import GENERATOR_PROMPT
from src.models.state import RAGState
from src.services.llm_factory import LLMFactory

logger = logging.getLogger(__name__)


def _format_context(chunks: list[dict]) -> str:
    """Format relevant chunks into a context string with source attribution."""
    parts = []
    for i, chunk in enumerate(chunks, 1):
        meta = chunk.get("metadata", {})
        source = meta.get("source_file", "Unknown")
        page = meta.get("page_number", "?")
        section = meta.get("section_header", "")
        header = f"[Source {i}: {source}, Page {page}]"
        if section:
            header += f" Section: {section}"
        parts.append(f"{header}\n{chunk['content']}")
    return "\n\n---\n\n".join(parts)


def generator_node(state: RAGState) -> dict:
    """Generate an answer from relevant chunks using GPT-4o-mini."""
    query = state.get("sanitized_query", "")
    chunks = state.get("relevant_chunks", [])

    context = _format_context(chunks)
    prompt = GENERATOR_PROMPT.format(context=context, query=query)

    try:
        llm = LLMFactory.get_generator_llm()
        result = llm.invoke([
            SystemMessage(content="You are a precise financial analyst assistant."),
            HumanMessage(content=prompt),
        ])
        logger.info(f"Generated answer: {len(result.content)} chars")
        return {"generated_answer": result.content}
    except Exception as e:
        logger.error(f"Generation failed: {e}")
        return {"generated_answer": "I encountered an error generating a response. Please try again."}
