import logging

from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI

from src.config.settings import settings

logger = logging.getLogger(__name__)


def _openai_fallback(model: str, temperature: float) -> ChatOpenAI:
    return ChatOpenAI(model=model, temperature=temperature, api_key=settings.OPENAI_API_KEY)


class LLMFactory:
    """Creates LLM instances with automatic fallback between providers.

    When settings.FORCE_OPENAI_ONLY is true, every method returns an OpenAI model
    regardless of Groq availability. This is used to run evaluation against the full
    pipeline without hitting Groq's free-tier rate limits.
    """

    @staticmethod
    def get_router_llm():
        """Llama on Groq for routing (free). Falls back to GPT-4o-mini."""
        if settings.FORCE_OPENAI_ONLY:
            return _openai_fallback(settings.GENERATOR_MODEL, 0)
        if settings.GROQ_API_KEY:
            return ChatGroq(model=settings.ROUTER_MODEL, temperature=0, api_key=settings.GROQ_API_KEY)
        logger.warning("Groq API key not set for router, falling back to OpenAI")
        return _openai_fallback(settings.GENERATOR_MODEL, 0)

    @staticmethod
    def get_grader_llm():
        """Llama on Groq for grading (free). Falls back to GPT-4o-mini."""
        if settings.FORCE_OPENAI_ONLY:
            return _openai_fallback(settings.GENERATOR_MODEL, 0)
        if settings.GROQ_API_KEY:
            return ChatGroq(model=settings.GRADER_MODEL, temperature=0, api_key=settings.GROQ_API_KEY)
        logger.warning("Groq API key not set for grader, falling back to OpenAI")
        return _openai_fallback(settings.GENERATOR_MODEL, 0)

    @staticmethod
    def get_generator_llm():
        """GPT-4o-mini for generation (quality). Falls back to Llama on Groq."""
        if settings.FORCE_OPENAI_ONLY:
            return _openai_fallback(settings.GENERATOR_MODEL, 0.1)
        if settings.OPENAI_API_KEY:
            return _openai_fallback(settings.GENERATOR_MODEL, 0.1)
        logger.warning("OpenAI API key not set for generator, falling back to Groq")
        return ChatGroq(model=settings.ROUTER_MODEL, temperature=0.1, api_key=settings.GROQ_API_KEY)

    @staticmethod
    def get_hallucination_llm():
        """GPT-4o-mini for hallucination checking. Falls back to Llama on Groq."""
        if settings.FORCE_OPENAI_ONLY:
            return _openai_fallback(settings.HALLUCINATION_MODEL, 0)
        if settings.OPENAI_API_KEY:
            return _openai_fallback(settings.HALLUCINATION_MODEL, 0)
        logger.warning("OpenAI API key not set for hallucination checker, falling back to Groq")
        return ChatGroq(model=settings.ROUTER_MODEL, temperature=0, api_key=settings.GROQ_API_KEY)
