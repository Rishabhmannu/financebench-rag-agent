"""Dense-embedding service. Dispatches to the configured provider.

Provider is selected via `settings.EMBEDDING_PROVIDER`:
  - "openai" (default): text-embedding-3-{small,large}
  - "voyage": voyage-finance-2 (uses voyageai SDK, input_type for query/doc)
  - "abaci":  AbaciNLP-hosted Fin-E5 (OpenAI-compatible API at
              https://abacinlp.com/v1, queries get an "Instruct: ...\\nQuery:"
              prefix, documents are embedded as-is)

The defaults match the call-site convention: `embed_text` is used for query
embedding at retrieval time, `embed_texts` is used for corpus indexing.

Voyage's SDK does not auto-retry transient errors, so we wrap calls with
exponential backoff for rate limits, 5xx, timeouts, and network drops.
AbaciNLP uses OpenAI's SDK which has built-in retries.
"""
from __future__ import annotations

import logging
import time

from openai import OpenAI

from src.config.settings import settings

logger = logging.getLogger(__name__)

_openai_client: OpenAI | None = None
_voyage_client = None  # voyageai.Client, lazy-imported
_abaci_client: OpenAI | None = None

VOYAGE_MAX_RETRIES = 5
VOYAGE_INITIAL_BACKOFF_S = 2.0

ABACI_BASE_URL = "https://abacinlp.com/v1"
ABACI_QUERY_INSTRUCTION = (
    "Given a financial question, retrieve relevant passages that answer the query."
)


def _get_openai_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=settings.OPENAI_API_KEY)
    return _openai_client


def _get_voyage_client():
    global _voyage_client
    if _voyage_client is None:
        import voyageai

        _voyage_client = voyageai.Client(api_key=settings.VOYAGE_API_KEY)
    return _voyage_client


def _get_abaci_client() -> OpenAI:
    global _abaci_client
    if _abaci_client is None:
        _abaci_client = OpenAI(
            api_key=settings.ABACI_NLP_API_KEY,
            base_url=ABACI_BASE_URL,
        )
    return _abaci_client


def _abaci_format_query(text: str) -> str:
    """AbaciNLP retrieval pattern: queries get the Instruct prefix; docs don't."""
    return f"Instruct: {ABACI_QUERY_INSTRUCTION}\nQuery: {text}"


def _abaci_embed(texts: list[str], input_type: str) -> list[list[float]]:
    """Call AbaciNLP via the OpenAI-compatible client.

    For input_type="query" each text is wrapped with the Instruct prefix.
    For input_type="document" texts are passed as-is.
    """
    if input_type == "query":
        formatted = [_abaci_format_query(t) for t in texts]
    else:
        formatted = texts
    response = _get_abaci_client().embeddings.create(
        input=formatted, model=settings.EMBEDDING_MODEL
    )
    return [item.embedding for item in response.data]


def _voyage_embed(texts: list[str], input_type: str) -> list[list[float]]:
    """Call Voyage embed with exponential backoff on transient errors.

    Retryable: rate limit (429), 5xx, timeout, connection drop.
    Non-retryable (re-raised): auth (401), invalid request (400).
    """
    import voyageai.error as ve

    backoff = VOYAGE_INITIAL_BACKOFF_S
    last_err: Exception | None = None
    for attempt in range(1, VOYAGE_MAX_RETRIES + 1):
        try:
            response = _get_voyage_client().embed(
                texts=texts,
                model=settings.EMBEDDING_MODEL,
                input_type=input_type,
            )
            return response.embeddings
        except (
            ve.RateLimitError,
            ve.ServerError,
            ve.ServiceUnavailableError,
            ve.Timeout,
            ve.TryAgain,
            ve.APIConnectionError,
        ) as err:
            last_err = err
            if attempt == VOYAGE_MAX_RETRIES:
                break
            logger.warning(
                "Voyage transient error (attempt %d/%d): %s — retrying in %.1fs",
                attempt, VOYAGE_MAX_RETRIES, type(err).__name__, backoff,
            )
            time.sleep(backoff)
            backoff *= 2
    raise RuntimeError(
        f"Voyage embed failed after {VOYAGE_MAX_RETRIES} attempts: {last_err}"
    )


def embed_text(text: str, input_type: str = "query") -> list[float]:
    """Embed a single text string. Returns a vector of floats.

    Sprint 8e: query embeddings (input_type="query") are cached by
    (provider, model, input_type, text). Document embeddings are NOT
    cached — they're embedded once at ingest time and stored in Qdrant.
    """
    if input_type == "query":
        from src.services.result_cache import get_or_compute

        return get_or_compute(
            "query-emb",
            (
                settings.EMBEDDING_PROVIDER,
                settings.EMBEDDING_MODEL,
                input_type,
                text,
            ),
            lambda: _embed_one_uncached(text, input_type),
        )
    return _embed_one_uncached(text, input_type)


def _embed_one_uncached(text: str, input_type: str) -> list[float]:
    if settings.EMBEDDING_PROVIDER == "voyage":
        return _voyage_embed([text], input_type=input_type)[0]
    if settings.EMBEDDING_PROVIDER == "abaci":
        return _abaci_embed([text], input_type=input_type)[0]
    response = _get_openai_client().embeddings.create(
        input=text, model=settings.EMBEDDING_MODEL
    )
    return response.data[0].embedding


def embed_texts(texts: list[str], input_type: str = "document") -> list[list[float]]:
    """Embed multiple texts in a single API call."""
    if settings.EMBEDDING_PROVIDER == "voyage":
        return _voyage_embed(texts, input_type=input_type)
    if settings.EMBEDDING_PROVIDER == "abaci":
        return _abaci_embed(texts, input_type=input_type)
    response = _get_openai_client().embeddings.create(
        input=texts, model=settings.EMBEDDING_MODEL
    )
    return [item.embedding for item in response.data]
