"""LLM factory with provider selection and fallback chains.

Per-task provider strategy (Sprint 7.9 Day 3 — heterogeneous model tiering):

| Task                    | Primary                  | Fallback       | Tier rationale |
|-------------------------|--------------------------|----------------|----------------|
| Router                  | Groq Llama 3.3 70B       | OpenAI 4o-mini | Latency-critical classification |
| Grader                  | Groq Llama 3.3 70B       | OpenAI 4o-mini | High-volume binary classification (8 calls/query) |
| Entity extractor LLM    | Groq Llama 3.3 70B       | OpenAI 4o-mini | Ambiguous-query fallback only |
| Query contextualizer    | (uses router LLM)        | —              | Same budget / latency profile |
| **Generator**           | **Claude Sonnet 4.6**    | OpenAI 4o-mini | User-facing quality; prompt-caching-friendly |
| **Hallucination checker**       | **Claude Haiku 4.5**     | OpenAI 4o-mini | Sprint 7.9 downgrade — verification fits Haiku's range; -$1.35/eval |
| **High-stakes hallucination (HITL)** | **Claude Sonnet 4.6** | Sonnet 4.6 | Sprint 7.9 — dropped Opus 4.7 per Vectara: Sonnet has *lower* hallucination than Opus on verification |
| **Research-agent: decompose**   | **gpt-4o-mini**          | OpenAI 4o-mini | Sprint 7.9 downgrade — 3-field structured classifier; -$0.55/eval |
| **Research-agent: sufficiency** | **gpt-4o-mini**          | OpenAI 4o-mini | Sprint 7.9 downgrade — 4-field judge; -$0.55/eval |
| **Research-agent: synthesize**  | **Claude Sonnet 4.6**    | OpenAI 4o-mini | Sprint 7.9 KEPT — Haiku 4.5 caused -1pp regression below dev-set noise floor |

Sprint 7.9 Day 2.5 finding (worth carrying forward): the n=30 dev-set has a
noise floor of roughly ±3 net pass-count delta even on identical settings. Day 3
shipped the 3 downgrades that matched the noise floor (drop-in safe) and dropped
the 1 that fell -1 below it. Net per-eval cost projection: $9.89 → ~$7.14 (~28%
reduction). Going forward, dev-set deltas in [-3, +1] should be treated as
within noise; only ≤ -4 with new regression patterns or ≥ +2 are decisive.

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
    """OpenAI chat model. When `LITELLM_URL` is set, routes through the LiteLLM
    proxy at `LITELLM_URL/v1` (Sprint 8 8a Day 2); otherwise direct to OpenAI.
    The proxy uses the container's own OPENAI_API_KEY so the api_key arg is a
    placeholder ("sk-litellm-proxy") — the langchain client requires *some*
    value but the proxy ignores it.
    """
    kwargs = {
        "model": model,
        "temperature": temperature,
        "callbacks": [get_cost_handler()],
    }
    if settings.LITELLM_URL:
        kwargs["base_url"] = f"{settings.LITELLM_URL.rstrip('/')}/v1"
        kwargs["api_key"] = "sk-litellm-proxy"
    else:
        kwargs["api_key"] = settings.OPENAI_API_KEY
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    return ChatOpenAI(**kwargs)


# Models that have deprecated the `temperature` parameter — passing it raises
# 400 Bad Request. Confirmed via Sprint 7.9 Day 1 API key swap smoke.
# Forward-safe: extend this set as Anthropic deprecates `temperature` on more
# models; the factory will silently omit the param.
_ANTHROPIC_NO_TEMPERATURE_MODELS: set[str] = {"claude-opus-4-7"}


def _anthropic(model: str, temperature: float = 0.0, max_tokens: int = 1024) -> ChatAnthropic:
    """Anthropic chat model. Prompt caching is applied at the message level by
    callers that want it — see `src.graph.nodes.generator` and
    `src.graph.nodes.hallucination`.

    `temperature` is silently omitted for models in `_ANTHROPIC_NO_TEMPERATURE_MODELS`
    (e.g. Opus 4.7, which deprecated the parameter). This keeps the factory
    forward-safe even when Sprint 7.9 rotates HITL off Opus 4.7 to Sonnet 4.6.

    Sprint 8 8a Day 2: when `LITELLM_URL` is set, routes through LiteLLM's
    Anthropic pass-through at `LITELLM_URL/anthropic` (preserves langchain-
    anthropic's native cache_control format end-to-end). Container holds
    the real ANTHROPIC_API_KEY; we pass a placeholder.
    """
    kwargs = {
        "model_name": model,
        "max_tokens": max_tokens,
        "callbacks": [get_cost_handler()],
    }
    if settings.LITELLM_URL:
        kwargs["base_url"] = f"{settings.LITELLM_URL.rstrip('/')}/anthropic"
        kwargs["api_key"] = "sk-litellm-proxy"
    else:
        kwargs["api_key"] = settings.ANTHROPIC_API_KEY
    if model not in _ANTHROPIC_NO_TEMPERATURE_MODELS:
        kwargs["temperature"] = temperature
    return ChatAnthropic(**kwargs)


def _llm_for_task(model_name: str, temperature: float = 0.0, max_tokens: int = 2048):
    """Dispatch to the right provider based on the model-name prefix.

    Used by Sprint 7.9 per-task model settings (decompose, sufficiency,
    synthesize) so each can be configured independently to claude-* or gpt-*
    without changing call sites.
    """
    if model_name.startswith("claude-"):
        return _anthropic(model_name, temperature=temperature, max_tokens=max_tokens)
    if model_name.startswith("gpt-"):
        return _openai(model_name, temperature=temperature, max_tokens=max_tokens)
    raise ValueError(
        f"Unknown model provider for {model_name!r}. "
        f"Expected prefix 'claude-' or 'gpt-'."
    )


def _groq(model: str, temperature: float = 0.0) -> ChatGroq | ChatOpenAI:
    """Groq chat model. When `LITELLM_URL` is set, routes through LiteLLM's
    OpenAI-compatible endpoint (Groq is exposed as a regular model in
    LiteLLM's model_list, no separate /groq pass-through needed).

    Returns `ChatOpenAI` instead of `ChatGroq` when proxied — same `.invoke()`
    interface, langchain-groq isn't needed when the proxy is doing the
    provider routing.
    """
    if settings.LITELLM_URL:
        return ChatOpenAI(
            model=model,
            temperature=temperature,
            base_url=f"{settings.LITELLM_URL.rstrip('/')}/v1",
            api_key="sk-litellm-proxy",
            callbacks=[get_cost_handler()],
        )
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

        max_tokens=2048 for Claude (Sprint 7.6 Day 4 fix): the research-agent
        synthesizer also calls this LLM and emits structured-markdown findings
        that can run 800-1200 tokens. 1024 was tight for agent-augmented
        answers (Day 4 partial run hit max_tokens stops on long syntheses).
        """
        if settings.FORCE_OPENAI_ONLY:
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.1)
        if settings.ANTHROPIC_API_KEY:
            return _anthropic(settings.GENERATOR_MODEL, temperature=0.1, max_tokens=2048)
        logger.warning("Anthropic API key not set for generator, falling back to OpenAI")
        return _openai(settings.OPENAI_FALLBACK_MODEL, 0.1)

    @staticmethod
    def get_hallucination_llm():
        """Standard grounding verification. Claude Sonnet 4.6 primary.

        max_tokens history:
          - 512: original; failed on every Sprint 7.5 Claude run (truncated
            mid-explanation, Pydantic validation failed → fallback to grounded)
          - 1024: Sprint 7.6 Day 1 fix; sufficient for fast-path answers
            (~325 output tokens avg in smoke).
          - 2048: Sprint 7.6 Day 4 fix; agent-augmented answers feed the
            checker 8-15 chunks plus a long structured answer, so the
            HallucinationCheck explanation field needs more room. Day 4
            partial run hit max_tokens stops at 1024.
        OpenAI fallback stays at 512 — its tool-call output is more compact
        and 512 was sufficient on prior Sprint 7.5 runs.
        """
        if settings.FORCE_OPENAI_ONLY:
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0, max_tokens=512)
        if settings.ANTHROPIC_API_KEY:
            return _anthropic(settings.HALLUCINATION_MODEL, temperature=0.0, max_tokens=2048)
        _warn_once(
            "hallucination_no_anthropic",
            "Anthropic API key not set for hallucination checker, falling back to OpenAI",
        )
        return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0, max_tokens=512)

    @staticmethod
    def get_high_stakes_hallucination_llm():
        """High-stakes hallucination check — fires when HITL threshold is triggered
        (dollar amounts above role's `requires_hitl_above`).

        Default model is `claude-opus-4-7` for backward compatibility. Sprint 7.9
        recommends rotating to `claude-sonnet-4-6` per Vectara hallucination
        benchmarks (Sonnet 4.6 has *lower* hallucination rate than Opus 4.6 on
        verification tasks; Opus 4.7 is positioned for complex agentic / coding
        work, not verification). Override via `HIGH_STAKES_HALLUCINATION_MODEL=claude-sonnet-4-6`.
        The `_anthropic` helper handles the Opus 4.7 `temperature`-deprecation
        edge case, so either model works without code changes.
        """
        if settings.FORCE_OPENAI_ONLY:
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0, max_tokens=512)
        if settings.ANTHROPIC_API_KEY:
            return _anthropic(settings.HIGH_STAKES_HALLUCINATION_MODEL, temperature=0.0, max_tokens=2048)
        _warn_once(
            "high_stakes_no_anthropic",
            "Anthropic API key not set for high-stakes check, falling back to OpenAI",
        )
        return LLMFactory.get_hallucination_llm()

    @staticmethod
    def get_research_decompose_llm():
        """Research-agent decompose step — Sprint 7.9 candidate downgrade.

        Pure structured-output classification (3 fields: qualifiers,
        required_quantities, sub_questions). Default `claude-sonnet-4-6`;
        Sprint 7.9 Workstream A tests `gpt-4o-mini` (~20× cheaper, fits
        the task complexity).
        """
        if settings.FORCE_OPENAI_ONLY:
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0, max_tokens=2048)
        if not settings.ANTHROPIC_API_KEY and settings.RESEARCH_AGENT_DECOMPOSE_MODEL.startswith("claude-"):
            _warn_once("decompose_no_anthropic", "Anthropic key missing, falling back to OpenAI for decompose")
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0, max_tokens=2048)
        return _llm_for_task(settings.RESEARCH_AGENT_DECOMPOSE_MODEL, temperature=0.0, max_tokens=2048)

    @staticmethod
    def get_research_sufficiency_llm():
        """Research-agent sufficiency-judge step — Sprint 7.9 candidate downgrade.

        4-field structured judge (decision, missing_quantity, follow_up_question,
        reason). Default `claude-sonnet-4-6`; Sprint 7.9 tests `gpt-4o-mini`.
        """
        if settings.FORCE_OPENAI_ONLY:
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0, max_tokens=2048)
        if not settings.ANTHROPIC_API_KEY and settings.RESEARCH_AGENT_SUFFICIENCY_MODEL.startswith("claude-"):
            _warn_once("sufficiency_no_anthropic", "Anthropic key missing, falling back to OpenAI for sufficiency")
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.0, max_tokens=2048)
        return _llm_for_task(settings.RESEARCH_AGENT_SUFFICIENCY_MODEL, temperature=0.0, max_tokens=2048)

    @staticmethod
    def get_research_synthesize_llm():
        """Research-agent synthesize step — Sprint 7.9 candidate downgrade.

        Generates the structured-markdown context block for the main generator.
        Default `claude-sonnet-4-6`; Sprint 7.9 tests `claude-haiku-4-5` (riskier
        — Haiku may not adhere to structured-markdown format as cleanly as
        Sonnet; A/B-test required at dev-set level).
        """
        if settings.FORCE_OPENAI_ONLY:
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.1, max_tokens=2048)
        if not settings.ANTHROPIC_API_KEY and settings.RESEARCH_AGENT_SYNTHESIZE_MODEL.startswith("claude-"):
            _warn_once("synthesize_no_anthropic", "Anthropic key missing, falling back to OpenAI for synthesize")
            return _openai(settings.OPENAI_FALLBACK_MODEL, 0.1, max_tokens=2048)
        return _llm_for_task(settings.RESEARCH_AGENT_SYNTHESIZE_MODEL, temperature=0.1, max_tokens=2048)
