import logging

from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI

from src.config.settings import settings

logger = logging.getLogger(__name__)


class LLMFactory:
    """Creates LLM instances with automatic fallback between providers."""

    @staticmethod
    def get_router_llm():
        """Llama on Groq for routing (free). Falls back to GPT-4o-mini."""
        if settings.GROQ_API_KEY:
            return ChatGroq(model=settings.ROUTER_MODEL, temperature=0, api_key=settings.GROQ_API_KEY)
        logger.warning("Groq API key not set for router, falling back to OpenAI")
        return ChatOpenAI(model=settings.GENERATOR_MODEL, temperature=0, api_key=settings.OPENAI_API_KEY)

    @staticmethod
    def get_grader_llm():
        """Qwen on Groq for grading (free). Falls back to GPT-4o-mini."""
        if settings.GROQ_API_KEY:
            return ChatGroq(model=settings.GRADER_MODEL, temperature=0, api_key=settings.GROQ_API_KEY)
        logger.warning("Groq API key not set for grader, falling back to OpenAI")
        return ChatOpenAI(model=settings.GENERATOR_MODEL, temperature=0, api_key=settings.OPENAI_API_KEY)

    @staticmethod
    def get_generator_llm():
        """GPT-4o-mini for generation (quality). Falls back to Llama on Groq."""
        if settings.OPENAI_API_KEY:
            return ChatOpenAI(model=settings.GENERATOR_MODEL, temperature=0.1, api_key=settings.OPENAI_API_KEY)
        logger.warning("OpenAI API key not set for generator, falling back to Groq")
        return ChatGroq(model=settings.ROUTER_MODEL, temperature=0.1, api_key=settings.GROQ_API_KEY)

    @staticmethod
    def get_hallucination_llm():
        """GPT-4o-mini for hallucination checking. Falls back to Llama on Groq."""
        if settings.OPENAI_API_KEY:
            return ChatOpenAI(model=settings.HALLUCINATION_MODEL, temperature=0, api_key=settings.OPENAI_API_KEY)
        logger.warning("OpenAI API key not set for hallucination checker, falling back to Groq")
        return ChatGroq(model=settings.ROUTER_MODEL, temperature=0, api_key=settings.GROQ_API_KEY)
