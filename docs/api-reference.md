# API Reference

Base URL: `http://localhost:8000` (dev)

Interactive docs: `http://localhost:8000/docs` (FastAPI-generated OpenAPI)

## Authentication

All `/chat`, `/hitl`, and `/ingest` endpoints require `Authorization: Bearer <jwt>` header.

### POST `/auth/login`

Exchange username/password for a JWT.

**Request**:
```json
{
  "username": "finance",
  "password": "finance123"
}
```

**Response** (200):
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "role": "finance"
}
```

**Errors**:
- `401` — Invalid credentials

## Chat

### POST `/chat`

Run a query through the full RAG pipeline (blocking).

**Request**:
```json
{
  "message": "What was Apple's total revenue in fiscal year 2023?",
  "thread_id": "optional-uuid-for-multi-turn"
}
```

If `thread_id` is omitted, a new one is generated and returned. Pass the same `thread_id` on follow-up turns to preserve conversation context.

**Response** (200):
```json
{
  "response": "Apple's total revenue in fiscal year 2023 was $383,285 million [Source: 10k_apple_2023.pdf, Page 1].",
  "sources": [
    {
      "file": "10k_apple_2023.pdf",
      "page": 1,
      "section": "",
      "doc_type": "10k"
    }
  ],
  "confidence": 1.0,
  "requires_approval": false,
  "thread_id": "cc1840f8-db41-4394-9c40-ed2c97a998ac"
}
```

When `requires_approval` is `true`, the graph paused for HITL. The `sources` array is empty in this case — resolve approval via `/hitl/approve` or `/hitl/reject` to get the final sources.

### POST `/chat/stream`

Same request body as `/chat`. Returns a Server-Sent Events stream.

**Event types** (one JSON object per `data:` line):

| Type | Fields | Meaning |
|------|--------|---------|
| `node_start` | `node`, `label` | A graph node just started (e.g., "Searching documents") |
| `node_end` | `node` | A graph node finished |
| `token` | `content` | Streaming token from the generator LLM |
| `hitl_interrupt` | `thread_id`, `reason`, `answer_preview` | Graph paused for approval |
| `final` | `response`, `sources`, `confidence`, `requires_approval`, `thread_id` | Final response (end of stream) |
| `error` | `message` | Unrecoverable error during processing |

**Example client** (Python):
```python
async with httpx.AsyncClient() as client:
    async with client.stream("POST", f"{API}/chat/stream",
                              json={"message": "..."},
                              headers={"Authorization": f"Bearer {token}"}) as r:
        async for line in r.aiter_lines():
            if line.startswith("data:"):
                event = json.loads(line[5:].strip())
                print(event["type"], event)
```

## Human-in-the-Loop

Used when `requires_approval=true`. Both endpoints require the same JWT used to start the chat.

### POST `/hitl/approve`

Resume a paused graph with approval. Returns the final response.

**Request**:
```json
{"thread_id": "cc1840f8-db41-4394-9c40-ed2c97a998ac"}
```

**Response** (200):
```json
{
  "status": "approved",
  "thread_id": "cc1840f8-...",
  "response": "...",
  "sources": [...],
  "confidence": 0.92
}
```

**Errors**:
- `503` — HITL not available (no PostgresSaver checkpointer configured)
- `500` — Failed to resume graph (thread_id invalid or expired)

### POST `/hitl/reject`

Resume a paused graph with rejection. The graph routes to `blocked_response` and returns a canned denial message.

**Request**:
```json
{"thread_id": "cc1840f8-..."}
```

**Response** (200):
```json
{
  "status": "rejected",
  "thread_id": "cc1840f8-...",
  "response": "I'm unable to process this request..."
}
```

## Ingestion

### POST `/ingest`

Upload new documents to Qdrant. (Admin/finance only — enforced in route.)

See [src/api/routes/ingest.py](../src/api/routes/ingest.py) for the current signature. Typical usage is batch ingestion via CLI: `python scripts/ingest_documents.py --input data/raw/`.

## Health

### GET `/health`

**Response** (200):
```json
{"status": "ok"}
```

No auth required. Used by Docker/Kubernetes healthchecks.

## Error Shape

All error responses follow FastAPI's default:
```json
{"detail": "Human-readable error message"}
```

Common status codes:
- `401` — Missing or invalid JWT
- `403` — JWT valid but role lacks permission
- `422` — Request body validation failed (e.g., missing `message`)
- `500` — Unhandled server error (graph failure, LLM timeout)
- `503` — Service unavailable (PostgreSQL/Qdrant down)

## Rate Limits

None enforced at the API layer in dev. The free Groq tier applies its own per-minute limits; the `LLMFactory` catches these and falls back to OpenAI automatically.

## CORS

`allow_origins` comes from `settings.CORS_ORIGINS` (default `["*"]` in dev). In production, the `@model_validator` in [settings.py](../src/config/settings.py) blocks startup if `ENVIRONMENT=production` and `CORS_ORIGINS` contains `"*"`.
