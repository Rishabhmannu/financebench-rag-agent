from openai import OpenAI

from src.config.settings import settings

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


def embed_text(text: str) -> list[float]:
    """Embed a single text string. Returns a vector of floats."""
    response = _get_client().embeddings.create(input=text, model=settings.EMBEDDING_MODEL)
    return response.data[0].embedding


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed multiple texts in a single API call."""
    response = _get_client().embeddings.create(input=texts, model=settings.EMBEDDING_MODEL)
    return [item.embedding for item in response.data]
