"""LLM factory with provider selection and fallback chains.

Per-task provider strategy (Sprint 7b):

| Task                    | Primary                  | Fallback       | Why |
|-------------------------|--------------------------|----------------|-----|
| Router                  | Groq Llama 3.3 70B       | OpenAI 4o-mini | Latency-critical classification |
| Grader                  | Groq Llama 3.3 70B       | OpenAI 4o-mini | High-volume binary classification (8 calls/query) |
| Entity extractor LLM    | Groq Llama 3.3 70B       | OpenAI 4o-mini | Ambiguous-query fallback only; latency-sensitive |
| Query contextualizer    | (uses router LLM)        | —              | Same budget / latency profile |
| Generator               | **Claude Sonnet 4.6**    | OpenAI 4o-mini | User-facing quality; prompt-caching-friendly |
| Hallucination checker   | **Claude Sonnet 4.6**    | OpenAI 4o-mini | Nuanced grounding assessment |
| High-stakes hallucination (HITL-triggered) | **Claude Opus 4.7** | Sonnet 4.6 | Top-quality verification for dollar amounts ≥ HITL threshold |

The `FORCE_OPENAI_ONLY=true` env override bypasses Groq and Anthropic, routing
every call through OpenAI. Used during eval runs to control Anthropic spend and
avoid Groq free-tier rate limits.
"""

import logging

from langchain_anthropic import ChatAnthropic
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI

from src.config.settings import settings
from src.services.cost_tracker import get_cost_handler

logger = logging.getLogger(__name__)


def _openai(model: str, temperature: float = 0.0, max_tokens: int | None = None) -> ChatOpenAI:
    kwargs = {
        "model": model,
        "temperature": temperature,
        "api_key": settings.OPENAI_API_KEY,
        "callbacks": [get_cost_handler()],
    }
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    return ChatOpenAI(**kwargs)


def _anthropic(model: str, temperature: float = 0.0, max_tokens: int = 1024) -> ChatAnthropic:
    """Anthropic chat model. Prompt caching is applied at the message level by
    callers that want it — see `src.graph.nodes.generator` and
    `src.graph.nodes.hallucination`."""
    return ChatAnthropic(
        model_name=model,
        temperature=temperature,
        max_tokens=max_tokens,
        api_key=settings.ANTHROPIC_API_KEY,
        callbacks=[get_cost_handler()],
    )


def _groq(model: str, temperature: float = 0.0) -> ChatGroq:
    return ChatGroq(
        model=model,
        temperature=temperature,
        api_key=settings.GROQ_API_KEY,
        callbacks=[get_cost_handler()],
    )


_warned_keys: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    """Emit each warning once per process — fast-path nodes are called per-question."""
    if key not in _warned_keys:
        _warned_keys.add(key)
        logger.warning(msg)


class LLMFactory:
    """Creates LLM instances with automatic fallback between providers.

    When settings.FORCE_OPENAI_ONLY is true, every method returns an OpenAI model
    regardless of other provider availability — used for evaluation runs.
    """

    @staticmethod
    def get_router_llm():
        """Classification-tier model. Groq for latency; OpenAI fallback.

        Groq is skipped if FORCE_OPENAI_ONLY is true OR USE_GROQ_FAST_PATH is false.
        The latter is the right knob during long evals — it preserves Claude on the
        generator/hallucination path while pushing the high-volume router/grader
        traffic to OpenAI, avoiding the Groq free-tier 100k tokens-per-day cap.
        """
        if settings.FORCE_OPENAI_ONLY or not settings.USE_GROQ_FAST_PATH:
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0)
        if settings.GROQ_API_KEY:
            return _groq(settings.ROUTER_MODEL, 0.0)
        _warn_once("router_no_groq", "Groq API key not set for router, falling back to OpenAI")
        return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0)

    @staticmethod
    def get_grader_llm():
        """Binary classification LLM — called once per chunk. Groq for throughput."""
        if settings.FORCE_OPENAI_ONLY or not settings.USE_GROQ_FAST_PATH:
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0)
        if settings.GROQ_API_KEY:
            return _groq(settings.GRADER_MODEL, 0.0)
        _warn_once("grader_no_groq", "Groq API key not set for grader, falling back to OpenAI")
        return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0)

    @staticmethod
    def get_generator_llm():
        """User-facing answer generator. Claude Sonnet 4.6 primary; OpenAI fallback.

        Claude's strength here is two-fold: (1) stronger instruction-following
        on 'only answer from context' prompts, which directly targets the
        faithfulness metric; (2) prompt caching on the system prompt + doc
        context, which we configure at the message level in the generator node
        to cut 60-70% of input-token cost on repeated queries.
        """
        if settings.FORCE_OPENAI_ONLY:
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.1)
        if settings.ANTHROPIC_API_KEY:
            return _anthropic(settings.GENERATOR_MODEL, temperature=0.1, max_tokens=1024)
        logger.warning("Anthropic API key not set for generator, falling back to OpenAI")
        return _openai(settings.OPENAI_FALLBACK_MODEL, 0.1)

    @staticmethod
    def get_hallucination_llm():
        """Standard grounding verification. Claude Sonnet 4.6 primary.

        max_tokens raised to 1024 for Claude — its tool-call output for the
        HallucinationCheck schema (grounded + score + explanation) was
        truncating mid-explanation at 512, causing Pydantic validation to fail
        and the parser to return an empty 'grounded=True' fallback. OpenAI
        emits a more compact tool call so 512 was sufficient there.
        """
        if settings.FORCE_OPENAI_ONLY:
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0, max_tokens=512)
        if settings.ANTHROPIC_API_KEY:
            return _anthropic(settings.HALLUCINATION_MODEL, temperature=0.0, max_tokens=1024)
        _warn_once(
            "hallucination_no_anthropic",
            "Anthropic API key not set for hallucination checker, falling back to OpenAI",
        )
        return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0, max_tokens=512)

    @staticmethod
    def get_high_stakes_hallucination_llm():
        """Claude Opus 4.7 — used only when HITL threshold is triggered (dollar
        amounts above the role's requires_hitl_above). The extra cost is bounded
        to HITL-path answers, which are already the most expensive per-query
        (an interrupt + human review round-trip)."""
        if settings.FORCE_OPENAI_ONLY:
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0, max_tokens=512)
        if settings.ANTHROPIC_API_KEY:
            return _anthropic(settings.HIGH_STAKES_HALLUCINATION_MODEL, temperature=0.0, max_tokens=1024)
        _warn_once(
            "high_stakes_no_opus",
            "Falling back from Opus 4.7 to Sonnet 4.6 for high-stakes check",
        )
        return LLMFactory.get_hallucination_llm()
