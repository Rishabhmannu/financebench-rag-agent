"""Dense-embedding service. Dispatches to the configured provider.

Provider is selected via `settings.EMBEDDING_PROVIDER` ("openai" or "voyage").

Voyage exposes an `input_type` parameter that materially changes the produced
vector — "query" prepends a different instruction prefix than "document". The
defaults here match the call-site convention: `embed_text` is used for query
embedding at retrieval time, `embed_texts` is used for corpus indexing. For
OpenAI the parameter is silently ignored.

Voyage's SDK does not auto-retry transient errors, so we wrap calls with
exponential backoff for rate limits, 5xx, timeouts, and network drops.
"""
from __future__ import annotations

import logging
import time

from openai import OpenAI

from src.config.settings import settings

logger = logging.getLogger(__name__)

_openai_client: OpenAI | None = None
_voyage_client = None  # voyageai.Client, lazy-imported

VOYAGE_MAX_RETRIES = 5
VOYAGE_INITIAL_BACKOFF_S = 2.0


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
    """Embed a single text string. Returns a vector of floats."""
    if settings.EMBEDDING_PROVIDER == "voyage":
        return _voyage_embed([text], input_type=input_type)[0]
    response = _get_openai_client().embeddings.create(
        input=text, model=settings.EMBEDDING_MODEL
    )
    return response.data[0].embedding


def embed_texts(texts: list[str], input_type: str = "document") -> list[list[float]]:
    """Embed multiple texts in a single API call."""
    if settings.EMBEDDING_PROVIDER == "voyage":
        return _voyage_embed(texts, input_type=input_type)
    response = _get_openai_client().embeddings.create(
        input=texts, model=settings.EMBEDDING_MODEL
    )
    return [item.embedding for item in response.data]
