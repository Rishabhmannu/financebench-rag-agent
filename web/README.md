# RAG Agent — Next.js Frontend (Sprint 9)

Sprint 9 admin/chat UI that replaces the Sprint 1–8 Gradio app. Sits at `/web` inside the parent repo. The backend is unchanged; this is purely a frontend swap.

## Stack

- **Next.js 16.2.6** (App Router, Turbopack) + React 19 + TypeScript strict
- **Tailwind 4** + **shadcn/ui** (`base-nova` preset, neutral)
- **TanStack Query** (server state) · **Zustand** (client state) · **next-themes** (dark mode)
- **react-hook-form** + **zod** (forms + validation)
- **sonner** (toasts)

## Architecture — BFF (Backend for Frontend)

The browser **never talks to the FastAPI backend directly**. Every backend call goes through a Next.js route handler under `/api/*` that:

1. Reads the JWT from an httpOnly session cookie (the browser cannot see this cookie via JavaScript)
2. Adds it as a bearer header on the outgoing fetch to FastAPI
3. Returns the response to the browser

This keeps the JWT off the client and makes CORS a non-issue. The only thing exposed to the browser is the Next.js origin.

```
Browser  ──fetch /api/chat/stream──▶  Next.js BFF  ──Bearer token──▶  FastAPI :8000
   ▲                                       │
   └────── SSE stream pass-through ────────┘
```

### Auth gate

`src/proxy.ts` is the Next 16 equivalent of `middleware.ts`. It only checks for the *presence* of the session cookie before letting unauthenticated users hit `/`, `/chat`, or `/admin`. Authorization itself stays on the backend — every BFF call re-verifies the JWT.

## Setup

### Prerequisites

- Node 20+ (works on 23)
- Backend running at `http://localhost:8000` (see root README)

### First-time

```bash
cd web
npm install
cp .env.example .env.local      # edit only if the backend isn't on :8000
npm run dev                      # serves http://localhost:3002
```

### Why port 3002 and not 3000?

Port 3000 is taken by Sprint 8c's self-hosted Langfuse (`langfuse-web`). Port 3001 may have a leftover next-server on some setups. We default to 3002 to dodge both. Override via `next dev -p <port>` if needed.

Because of the BFF pattern, the frontend port doesn't need to be in the backend's `CORS_ORIGINS` — only the FastAPI origin (`http://localhost:8000`) is called by Next.js server code, never by the browser.

## Project layout

```
web/src/
  proxy.ts                    # Next 16 auth gate (formerly middleware.ts)
  app/
    layout.tsx                # ThemeProvider + QueryProvider + Toaster
    page.tsx                  # redirects to /chat
    login/page.tsx            # login form with dev-user quick-fill
    (app)/                    # route group — everything auth-gated lives here
      layout.tsx              # AppHeader wrapper
      chat/page.tsx           # chat UI
    api/                      # BFF route handlers
      auth/login/route.ts     # POST → FastAPI /auth/login, sets cookie
      auth/logout/route.ts    # POST → clears cookie
      auth/me/route.ts        # GET  → FastAPI /auth/me
      chat/stream/route.ts    # POST → FastAPI /chat/stream (SSE passthrough)
  lib/
    env.ts                    # centralized env access
    session.ts                # cookie helpers + unsafe JWT decode (display only)
    api.ts                    # server-side fetch wrapper for BFF routes
    api-types.ts              # hand-mirrored backend types — keep in sync
    utils.ts                  # cn helper (shadcn)
  components/
    theme-provider.tsx        # next-themes wrapper
    theme-toggle.tsx          # sun/moon button
    query-provider.tsx        # TanStack Query client
    app-header.tsx            # user dropdown + admin link + theme + logout
    chat/
      chat-message.tsx        # one user/assistant bubble + sources/status
      chat-input.tsx          # autosizing textarea + send/stop button
      node-status.tsx         # "Generating answer…" spinner pill
      source-chips.tsx        # citation badges linking to /api/documents/{file}
    ui/                       # shadcn primitives
  hooks/
    use-stream-chat.ts        # the SSE consumer state machine
```

## Backend contracts consumed

Hand-mirrored in [src/lib/api-types.ts](src/lib/api-types.ts). Re-generate from the live FastAPI server with:

```bash
npx openapi-typescript http://localhost:8000/openapi.json -o src/lib/api-types.gen.ts
```

| Endpoint | Used by |
|---|---|
| `POST /auth/login` | `app/api/auth/login/route.ts` |
| `GET /auth/me` | `app/api/auth/me/route.ts` (called from `AppHeader` on mount) |
| `POST /chat/stream` (SSE) | `app/api/chat/stream/route.ts` → `useStreamChat` |
| `POST /hitl/approve` \| `/hitl/reject` | _Sprint 9.2 — pending_ |
| `GET /threads`, `GET /threads/{id}`, `DELETE /threads/{id}` | _Sprint 9.2 — pending_ |
| `GET /documents/{filename}` | Source chips link target (no in-browser viewer yet) |
| `GET /admin/costs`, `GET /admin/users`, `GET/POST/PATCH/DELETE /admin/roles` | _Sprint 9.4 — pending_ |
| `POST /ingest/upload` | _Sprint 9.5 — pending_ |

## SSE event shape

`POST /chat/stream` emits `data: {...}\n\n` frames. Each frame is one of:

```ts
| { type: "node_start"; node: string; label: string }
| { type: "node_end"; node: string }
| { type: "token"; content: string }                                     // generator only
| { type: "hitl_interrupt"; thread_id: string; reason: string; answer_preview: string }
| { type: "final"; response: string; sources: ChatSource[]; confidence: number | null; requires_approval: boolean; thread_id: string }
| { type: "error"; message: string }
```

The state machine in [src/hooks/use-stream-chat.ts](src/hooks/use-stream-chat.ts) consumes these into a `ChatTurn` per assistant message.

## Sprint 9 phases

- ✅ **9.0** — Backend prereqs (separate commit `2135ff4`): 11 new/modified endpoints, Alembic migrations, `roles` DB table, 294 tests
- ✅ **9.1** — Vertical slice (this directory): login → chat with SSE streaming + sources, user header, theme toggle, BFF auth, proxy gate
- 🚧 **9.2** — Thread sidebar (`/threads`) + HITL approval dialog (`/hitl/*`)
- 🚧 **9.3** — Citation PDF clickthrough (`react-pdf` in a Sheet)
- 🚧 **9.4** — Admin panel: `/admin/costs` Recharts dashboard, users table, roles CRUD
- 🚧 **9.5** — File upload UI, eval dashboard, Cmd+K palette, `web` service in docker-compose, delete Gradio

## Common pitfalls when running locally

### "Login failed" toast on every credential

The dev credentials (`admin/admin123`, `finance/finance123`, etc.) are correct. "Login failed" with no further detail almost always means **the BFF can't reach the backend**. The BFF will now surface a clearer message ("Backend unreachable at http://localhost:8000. Is the API server running?") so check the toast carefully.

Confirm the backend is up:

```bash
curl http://localhost:8000/health
```

### Chat hangs after the first node-status pill (or no pill appears at all)

Symptom: backend log shows the router and grader fired (`RouterDecision(...)` + a few `GradeResult(...)` warnings), then silence. Frontend shows "…" with no node label.

This is almost always an **LLM connectivity issue** in the generator node. Three things to check in the project root `.env`:

| Env var | Issue | Fix |
|---|---|---|
| `LITELLM_URL` | If set to `http://litellm:4000` (the docker-compose service hostname), uvicorn running directly on the host can't resolve `litellm` → DNS hang. | Comment it out for host-side uvicorn, OR set to `http://localhost:4000` if you separately started the LiteLLM container. |
| `ANTHROPIC_API_KEY` | Required for Sonnet 4.6 generator (Sprint 7.9 canonical). | Set a valid key. |
| `EMBEDDING_PROVIDER=voyage` | Requires `VOYAGE_API_KEY`. If you don't have one, switch provider. | `EMBEDDING_PROVIDER=openai` to use text-embedding-3-small instead. |

Isolate the frontend from the issue with a direct curl:

```bash
TOKEN=$(python scripts/generate_jwt.py --role admin --user-id admin)
curl -N -X POST http://localhost:8000/chat/stream \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "What was Apple FY2023 revenue?"}'
```

If that hangs too → backend env issue (fix `.env`, restart uvicorn).
If that streams cleanly → BFF/SSE buffering issue inside Next.js, separate fix.

### Port 3000 or 3001 conflicts

Port 3000 = Langfuse (Sprint 8c). Port 3001 has been seen with leftover Node processes. Default is 3002 (see `package.json`). Override in `package.json`'s `dev` and `start` scripts if needed.

### `useSearchParams() should be wrapped in a suspense boundary`

Next 16 enforces this at build time. If you add a page that reads `useSearchParams()`, wrap the consumer in `<Suspense>` (see [src/app/login/page.tsx](src/app/login/page.tsx) for the pattern).

### `Property 'asChild' does not exist on...`

The shadcn `base-nova` preset uses `@base-ui/react`, not Radix. There is no `asChild` prop. Two options:

1. Use the primitive's `render` prop instead (Base UI's compose pattern)
2. Skip the wrapper Button — render an `<a>` or `<button>` directly with `className=` matching the variant styles

See [src/components/app-header.tsx](src/components/app-header.tsx) for both patterns.

## Notes on Next.js 16

The auto-generated `AGENTS.md` correctly warns that Next 16 has breaking changes from earlier versions. Key things that surface in this code:

- `middleware.ts` → `proxy.ts` (renamed, same functionality)
- `cookies()` is async — `await cookies()` in route handlers
- `useSearchParams()` requires a `<Suspense>` boundary
- BFF route handlers via `app/api/*/route.ts` (no `pages/api`)

The full bundled docs live at `node_modules/next/dist/docs/` — read those rather than training-data Next 14/15 patterns.
