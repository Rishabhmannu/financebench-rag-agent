# API Reference

Base URL: `http://localhost:8000` (dev)

Interactive docs: `http://localhost:8000/docs` (FastAPI-generated OpenAPI)

## Authentication

All endpoints except `POST /auth/login` and `GET /health` require an `Authorization: Bearer <jwt>` header. JWTs are HS256, default 24h expiry; payload includes `sub` (user_id), `name`, `role`, `department`.

### POST `/auth/login`

Exchange username/password for a JWT plus the full identity tuple — so the frontend can render the user header on first paint without a separate `/auth/me` round-trip.

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
  "user_id": "finance",
  "name": "Test Finance",
  "role": "finance",
  "department": "FP&A"
}
```

**Errors**:
- `401` — Invalid credentials

### GET `/auth/me`

Return the current user's identity plus role-derived RBAC permissions. Frontend calls this on app boot (or after a token refresh) to re-hydrate user state without keeping the JWT payload as the client's source of truth. The permissions block lets the UI gate admin nav, upload buttons, HITL approval surfaces, etc.

**Response** (200):
```json
{
  "user_id": "finance",
  "name": "Test Finance",
  "role": "finance",
  "department": "FP&A",
  "permissions": {
    "allowed_doc_types": ["10k", "invoice", "expense_policy"],
    "allowed_confidentiality": ["public", "internal"],
    "max_results": 10,
    "requires_hitl_above": 100000
  }
}
```

`permissions` reflects the (DB-backed) `roles` table, so live admin-panel edits via `/admin/roles` propagate without a redeploy. `requires_hitl_above` is `null` for roles that never trigger HITL.

**Errors**:
- `401` — Missing or invalid JWT

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

## Threads (Conversation History)

The Sprint 9 frontend sidebar lists prior conversations and lets users resume them. Ownership is enforced by the `user_id` embedded in LangGraph's checkpoint metadata at chat-route time — cross-user access returns 403, not 404, so the API doesn't leak existence between users.

### GET `/threads`

List the caller's threads, newest first.

**Query**: `limit` (default 50, max 200), `offset` (default 0).

**Response** (200):
```json
{
  "threads": [
    {
      "thread_id": "cc1840f8-db41-...",
      "title": "What was Apple's FY2023 revenue?",
      "checkpoint_count": 4,
      "is_interrupted": false
    }
  ],
  "total": 12,
  "limit": 50,
  "offset": 0
}
```

`title` is the first user message captured in the earliest checkpoint, truncated to 80 chars. `is_interrupted=true` means the thread is paused at a HITL gate and needs approval before it can produce a final answer.

**Errors**:
- `401` — Missing or invalid JWT
- `503` — Checkpoint store not initialized (HITL/threads disabled)

### GET `/threads/{thread_id}`

Load messages + interrupt state for one thread.

**Response** (200):
```json
{
  "thread_id": "cc1840f8-...",
  "messages": [
    {"role": "user", "content": "What was Apple's FY2023 revenue?"},
    {
      "role": "assistant",
      "content": "Apple's FY2023 revenue was $383.3B...",
      "sources": [{"file": "10k_aapl_2023.pdf", "page": 12}],
      "confidence": 0.92
    }
  ],
  "is_interrupted": false,
  "interrupt_payload": null
}
```

When `is_interrupted=true`, `interrupt_payload` carries the pending HITL prompt:
```json
{"reason": "Amount exceeds $100,000", "answer_preview": "Q4 capex was $4.8 billion..."}
```

**Errors**:
- `403` — Thread belongs to a different user
- `404` — Thread not found
- `503` — Checkpoint store not initialized

### DELETE `/threads/{thread_id}`

Remove a thread from the checkpoint store (all three checkpoint tables). Owner or admin only.

**Response** (204): empty body.

**Errors**: same as GET, plus `403` if a non-admin tries to delete someone else's thread.

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

Ingest the contents of `data/sample/` into the active Qdrant collection. **Admin only.**

**Response** (200):
```json
{"status": "success", "chunks_ingested": 47, "files_processed": 8}
```

**Errors**:
- `403` — Non-admin caller
- `404` — `data/sample/` is missing or empty

### POST `/ingest/upload`

Accept one or more uploaded PDFs (multipart) and ingest them. **Admin only.**

**Request**: `multipart/form-data` with one or more `files` parts and optional form fields:
- `doc_type` (default `"10k"`)
- `confidentiality` (default `"public"`)

```bash
curl -X POST "${API}/ingest/upload" \
  -H "Authorization: Bearer ${TOKEN}" \
  -F "files=@10k_xyz_2023.pdf" \
  -F "files=@10k_abc_2023.pdf" \
  -F "doc_type=10k" -F "confidentiality=public"
```

**Response** (200):
```json
{
  "status": "success",
  "files_processed": 2,
  "chunks_ingested": 47,
  "files": [
    {"filename": "10k_xyz_2023.pdf", "chunks": 28},
    {"filename": "10k_abc_2023.pdf", "chunks": 19}
  ],
  "errors": []
}
```

`status` is one of:
- `"success"` — every file ingested cleanly
- `"partial"` — some files succeeded, others surface in `errors[]`
- `"failed"` — every file errored; `errors[]` lists details

Saved PDFs land at `DOCUMENTS_ROOT` (default `data/sample/`) so the `/documents/{filename}` endpoint can later serve them for citation clickthrough.

**Errors**:
- `400` — No files in the upload
- `403` — Non-admin caller

## Documents

### GET `/documents/{filename}`

Stream a PDF for in-browser viewing. The filename must match a `source_file` recorded on Qdrant chunks; the caller's role permissions are checked against the document's `doc_type` and `confidentiality` payload before the file is served.

Path-traversal is rejected: no `..`, separators, leading dots, or non-PDF extensions are permitted.

**Response** (200): `application/pdf` stream with `Content-Disposition: inline; filename="..."`.

**Errors**:
- `400` — Invalid filename (path traversal, non-PDF extension)
- `403` — Caller's role lacks access to this document's `doc_type` / `confidentiality`
- `404` — Document isn't indexed (no Qdrant chunk for it) or the file isn't on disk

## Admin

All `/admin/*` endpoints require the `admin` role. Non-admin callers get 403.

### GET `/admin/costs`

Aggregate LLM spend from the self-hosted Langfuse instance over a configurable window. Grouped three ways: by user, by model, by trace name (`litellm-acompletion` vs `litellm-aembedding` vs `litellm-pass_through_endpoint`).

**Query**: `days` (default 7, range 1–90)

**Response** (200):
```json
{
  "window_days": 7,
  "start": "2026-05-04T...",
  "end": "2026-05-11T...",
  "total": {"calls": 11185, "cost_usd": 6.7466, "tokens": 6512912},
  "by_user": [
    {"key": "finance", "calls": 412, "cost_usd": 0.84, "tokens": 124880},
    {"key": null, "calls": 9322, "cost_usd": 5.91, "tokens": 6201044}
  ],
  "by_model": [
    {"key": "claude-sonnet-4-6", "calls": 377, "cost_usd": 5.21, "tokens": 613081}
  ],
  "by_trace_name": [
    {"key": "litellm-acompletion", "calls": 750, "cost_usd": 6.30, "tokens": 1109654}
  ]
}
```

`by_user.key = null` rows are unattributed traces (scripts, eval runs, smokes) — anything that didn't pass through the FastAPI auth dependency.

### GET `/admin/users`

List configured users. Currently reads from the in-memory `DEV_USERS` dict in `src/api/routes/auth.py`; will swap to a DB-backed user store when that lands.

**Response** (200):
```json
{
  "users": [
    {"username": "analyst", "name": "Test Analyst", "role": "analyst", "department": "Research"},
    {"username": "finance", "name": "Test Finance", "role": "finance", "department": "FP&A"}
  ]
}
```

### GET `/admin/roles`

List every RBAC role from the (DB-backed) `roles` table. The frontend admin panel renders this as a table users can edit.

**Response** (200):
```json
{
  "roles": [
    {
      "name": "analyst",
      "allowed_doc_types": ["10k"],
      "allowed_confidentiality": ["public"],
      "max_results": 5,
      "requires_hitl_above": null,
      "is_system": true
    }
  ]
}
```

`is_system=true` roles (the 5 built-ins seeded by the initial migration) cannot be deleted but can have their permissions edited.

### POST `/admin/roles`

Create a new role.

**Request**:
```json
{
  "name": "auditor",
  "allowed_doc_types": ["10k", "invoice"],
  "allowed_confidentiality": ["public", "internal"],
  "max_results": 8,
  "requires_hitl_above": 50000
}
```

`name` must be unique. `is_system` is intentionally not accepted — system roles are only seeded by migrations.

**Response** (201): the created role object (same shape as `GET /admin/roles[].roles[i]`).

**Errors**:
- `409` — A role with this name already exists

### PATCH `/admin/roles/{name}`

Partial update; only fields present in the body are changed.

**Request** (e.g.):
```json
{"max_results": 20}
```

**Response** (200): the updated role.

**Errors**:
- `404` — Role doesn't exist

### DELETE `/admin/roles/{name}`

Hard-delete a role.

**Response** (204): empty body.

**Errors**:
- `404` — Role doesn't exist
- `409` — Role is `is_system=true` and protected

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
- `400` — Bad request shape (e.g., path traversal in filename)
- `401` — Missing or invalid JWT
- `403` — JWT valid but role lacks permission
- `404` — Resource not found
- `409` — Conflict (duplicate role, system-role deletion)
- `422` — Request body validation failed (e.g., missing required field)
- `500` — Unhandled server error (graph failure, LLM timeout)
- `502` — Upstream service error (Langfuse query failed)
- `503` — Service unavailable (PostgreSQL/Qdrant down, checkpoint store not init'd)

## Rate Limits

None enforced at the API layer in dev. The free Groq tier applies its own per-minute limits; the `LLMFactory` catches these and falls back to OpenAI automatically. LiteLLM (the proxy gateway when `LITELLM_URL` is set) layers its own `num_retries=2` and per-process budget caps from `litellm_config.yaml`.

## CORS

`allow_origins` comes from `settings.CORS_ORIGINS`. Default (`["http://localhost:3000","http://localhost:7860"]`) covers the Next.js dev server and the legacy Gradio app. In production, the `@model_validator` in [settings.py](../src/config/settings.py) blocks startup if `ENVIRONMENT=production` and `CORS_ORIGINS` contains `"*"`.
