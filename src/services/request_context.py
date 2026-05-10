"""Request-scoped context for cross-cutting concerns.

Populated by the FastAPI auth dependency (`get_current_user`) and read by
`LLMFactory` so every LLM call carries the requester's user id in the
OpenAI / Anthropic `user` body field. LiteLLM forwards that field to
Langfuse as `userId`, which is what the `/admin/costs` endpoint groups
by — Sprint 8 8d's per-user cost attribution depends on this thread.

Implemented as a `ContextVar` so the value is correctly scoped to the
async task handling each request, with no risk of bleeding between
concurrent requests on the same event loop.
"""
from contextvars import ContextVar

current_user_id: ContextVar[str | None] = ContextVar("current_user_id", default=None)
