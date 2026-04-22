# Research: UI Framework Assessment — From Gradio to Enterprise-Grade

*Research date: April 2026*

Your backend is already enterprise-grade (FastAPI with SSE, PostgresSaver, LangGraph interrupt-based HITL, JWT + RBAC, 152 tests). The UI is the only thing making it look like a research demo.

## TL;DR Recommendation

**Build with Next.js 15 (App Router) + assistant-ui + shadcn/ui + Vercel AI Elements, deployed as a separate container alongside your existing FastAPI.**

This is the shortest path from "looks like a demo" to "looks like a Series B SaaS," and it's the only stack on this list that natively understands LangGraph's `interrupt()` pattern you already use.

**Do not go monorepo.** Keep frontend and backend in separate repos/containers for a solo dev.

## Option-by-Option Scorecard

| Stack | Look (1-10) | Dev Effort | SSE | JWT Auth | HITL Fit | Ceiling | Learning Curve | Deploy |
|---|---|---|---|---|---|---|---|---|
| **Next.js + assistant-ui + shadcn** | **9.5** | 2-3 weeks | Native (LangGraph adapter) | Medium (BFF) | **Native `interrupt()` support** | Unlimited | Medium (React + TS) | Separate container |
| Next.js + Vercel AI SDK + AI Elements | 9.5 | 2-3 weeks | Native (`useChat`) | Medium | Manual wiring | Unlimited | Medium | Separate container |
| CopilotKit + LangGraph | 8 | 1-2 weeks | Native (CoAgents) | Medium | Built-in generative UI | High | Medium | Separate container |
| React + Mantine | 8.5 | 3-4 weeks | Manual (EventSource) | Medium | Build yourself | Unlimited (best dashboards) | Medium | Separate container |
| React + MUI | 7.5 | 3-4 weeks | Manual | Medium | Build yourself | Unlimited | Medium-high | Separate container |
| React + Ant Design | 7 | 3-4 weeks | Manual | Medium | Build yourself | Unlimited | Medium | Separate container |
| Chainlit | 7 | 3-5 days | Native | Built-in | **No native HITL** (open issue) | Low-medium | Very low (Python) | Single container |
| Streamlit | 5 | 2-3 days | `st.write_stream` | Manual | Build with session state | Low | Very low | Single container |
| Mesop | 6 | 1 week | Decent | Manual | Build yourself | Medium | Low (Python) | Single container |
| Vite + React + TanStack + shadcn | 9 | 3-4 weeks | Manual | Easier than Next (no SSR) | Build yourself | Unlimited | Medium | Separate container |

## Top 3 Ranked by "Professional Look Per Hour Invested"

### 1. Next.js 15 + assistant-ui + shadcn/ui (the winner)

**assistant-ui** has a first-class `@assistant-ui/react-langgraph` package that understands `client.runs.stream()`, LangChain message formats, and — critically — **LangGraph's `interrupt()` pattern with `Command(resume=...)`**, which is exactly what your `hitl_gate.py` emits today. You get thread persistence, auto-scroll, retries, markdown, code highlighting, attachments, and inline approval UI out of the box. The look is indistinguishable from Anthropic's Console or ChatGPT.

**Why assistant-ui over raw Vercel AI SDK**: you already have a LangGraph state machine with interrupts. Vercel AI SDK's `useChat` treats the backend as a stateless stream-producer; wiring interrupts and `Command(resume=...)` back through it is manual. assistant-ui did that work.

### 2. Next.js 15 + Vercel AI Elements (the pure-shadcn route)

Vercel's AI Elements is 25+ shadcn-based components (Conversation, Message, Reasoning, Tool, Source, CodeBlock, Actions, Suggestion, Branch, PromptInput) that install into your own codebase via shadcn CLI. You own every file. Pairs with `useChat` for streaming. **Downside**: you hand-roll the LangGraph interrupt flow. Plan ~3-4 extra days for HITL.

### 3. CopilotKit (fastest to "good enough")

CoAgents v0.2 has official LangGraph Python integration, shared state between graph and UI, and built-in generative UI for tool calls. Ships a `<CopilotChat />` React component. Less design freedom than shadcn — the aesthetic is "a CopilotKit app," fine but not as bespoke.

## Concrete Component Recipe

**Core chat surface**
- `assistant-ui` Thread, Composer, MessageList, ActionBar
- `@assistant-ui/react-langgraph` for the runtime
- shadcn: Button, Card, Dialog, DropdownMenu, Tooltip, Toast (Sonner), Skeleton, ScrollArea, Tabs, Separator, Avatar, Badge

**Auth and shell**
- shadcn: Sheet (mobile nav), Sidebar (conversation history), Command (Cmd+K), Form + Input + Label (login)
- `next-themes` for dark/light toggle
- `zustand` for client state (current thread, role, filters)
- `@tanstack/react-query` for REST calls

**HITL approval panel**
- shadcn AlertDialog for the approval modal
- Display `State.interrupt.value` from `graph.aget_state()` — your backend already surfaces this
- Two buttons calling `/hitl/approve` and `/hitl/reject`, which trigger `Command(resume=...)` on your side

**Citations / PDF clickthrough**
- `react-pdf` for inline PDF rendering at specific pages (use `pageNumber` from your Qdrant payload)
- shadcn Hover Card for citation preview on hover
- shadcn Sheet opens PDF viewer docked right

**Admin panel**
- `@tanstack/react-table` + shadcn Table primitives for Users/Roles grid
- shadcn Form + zod for role creation
- shadcn Switch for toggling doc_type permissions

**Charts** (for eval dashboard)
- **Recharts** — shadcn's official chart wrapper is built on it and matches the design system. Don't use Chart.js or Nivo; they won't theme cleanly.

**Keyboard shortcuts**
- `cmdk` (already used by shadcn Command) for command palette
- `react-hotkeys-hook` for `Cmd+K`, `Cmd+Enter`, `Cmd+/`

**File upload**
- shadcn + `react-dropzone` for drag/drop; POST to `/ingest` FastAPI route

## The Monorepo Question — Don't Do It

For a solo dev, a Turborepo with Next.js + FastAPI is an attractive trap:

- **Python and Node tooling don't share caches.** Turborepo caches TS/JS tasks; Python changes don't benefit.
- **Dockerfile multiplication.** Same two images plus a root Dockerfile with confusing build contexts.
- **Deploy coupling.** CI rebuilds everything when only the frontend changed.
- **Onboarding friction.** If you contract help, the Python and React devs each edit in a shared repo where half the tools don't apply.

**Do this instead**: two repos (or `backend/` and `frontend/` folders if you must, but separate docker-compose services, separate CI workflows, separate deploys). Deploy FastAPI to ECS Fargate (you already have the workflow). Deploy Next.js to **Vercel** (zero-config) or a second ECS service if in-VPC. Use `NEXT_PUBLIC_API_URL` to point at FastAPI.

**One thing worth sharing across the boundary**: OpenAPI-generated TS types. Add `openapi-typescript` to frontend; run against FastAPI's `/openapi.json` in a `pnpm generate:types` script. Your React now knows every Pydantic model for free.

## JWT + Auth — The BFF Pattern

1. User POSTs `/api/login` to a **Next.js route handler** (not directly to FastAPI).
2. Handler calls FastAPI `/auth/login`, receives JWT, sets it as `httpOnly; secure; sameSite=lax` cookie on Next.js domain.
3. Client calls hit Next.js route handlers, which read the cookie and forward to FastAPI with `Authorization: Bearer ...`.
4. Next.js `middleware.ts` checks cookie and redirects unauthenticated users to `/login`.

Avoids CORS pain, keeps JWT unreachable from XSS. Estimated effort: 1 day.

## HITL UX — The Part Nobody Else Gets Right

Your `hitl_gate.py` uses `interrupt()` with a threshold, and `/hitl/approve` uses `Command(resume=...)`. assistant-ui's LangGraph adapter treats interrupts as first-class events:

1. Graph streams events → user sees the assistant typing.
2. `hitl_gate` fires `interrupt()` → assistant-ui receives the event and stops the streaming indicator.
3. Render an inline "Approval required" card showing reason + dollar amount from `interrupt.value`.
4. Approve button calls `/hitl/approve`; adapter sends `Command(resume={"approved": true})` and resumes the same thread.
5. Stream continues into response_formatter.

Whole loop is ~40 lines of React. **Your backend needs zero changes** — it already emits everything assistant-ui expects.

## Migration Path From Gradio

**Keep**: everything in `src/` except `src/frontend/`. Your FastAPI routes, LangGraph builder, guardrails, RBAC, Qdrant pipeline, eval suite, Docker setup for backend services — all unchanged.

**Rewrite**: only `src/frontend/gradio_app.py`. Replace with a new `web/` directory containing the Next.js app. SSE endpoint contract stays identical; only the consumer changes.

**Add**: one new FastAPI route for `/admin/roles` CRUD (for the admin panel), one `/ingest/upload` route for ad-hoc file upload.

**Phased plan (solo dev, ~3 weeks)**:
- **Week 1**: Scaffold Next.js + shadcn + assistant-ui, wire login + JWT cookie, connect to existing `/chat/stream` SSE, verify basic chat end-to-end.
- **Week 2**: HITL approval UI, conversation history sidebar (needs new `/threads` route listing checkpointer entries), citations with PDF viewer, dark mode, Cmd+K.
- **Week 3**: Admin panel (create roles, assign access), file upload, eval dashboard with Recharts, polish (empty states, skeletons, error boundaries), deploy.

## Production Reference Apps Worth Studying

- **[Onyx](https://github.com/onyx-dot-app/onyx)** — MIT-licensed enterprise RAG platform. Closest analogue to your project. Next.js frontend.
- **[Kotaemon](https://github.com/Cinnamon/kotaemon)** — Clean RAG UI with citation clickthrough.
- **[assistant-ui-starter-langgraph](https://github.com/assistant-ui/assistant-ui-starter-langgraph)** — Official starter. Clone this first.
- **[Vercel AI Chatbot](https://github.com/vercel/ai-chatbot)** — AI SDK + shadcn + auth + persistence reference.
- **[Verba](https://github.com/weaviate/Verba)** — Weaviate's RAG chatbot; ingestion-UI patterns.

## What To Do Monday Morning

```bash
npx create-next-app@latest web --typescript --tailwind --app
cd web
npx shadcn@latest init
npx shadcn@latest add button card dialog dropdown-menu sheet sidebar command form input label avatar badge scroll-area tabs separator skeleton sonner alert-dialog hover-card switch table
npx ai-elements@latest add
npm i @assistant-ui/react @assistant-ui/react-langgraph @langchain/langgraph-sdk
npm i @tanstack/react-query zustand next-themes react-hotkeys-hook react-pdf recharts openapi-typescript
```

Then clone the assistant-ui LangGraph starter for reference, point its streaming URL at your existing FastAPI, and you should see your graph responding inside a professional-looking thread within a few hours.

## What You're Trading Away

Moving off Gradio means you lose: instant sharing via `share=True`, zero-config deploy, ability to iterate in one Python file. For an internal research tool those matter; for an enterprise RAG product with RBAC + admin panels + HITL, they don't.

### Sources
- [assistant-ui official](https://www.assistant-ui.com)
- [assistant-ui LangGraph runtime docs](https://www.assistant-ui.com/docs/runtimes/langgraph)
- [@assistant-ui/react-langgraph npm](https://www.npmjs.com/package/@assistant-ui/react-langgraph)
- [Vercel AI Elements announcement](https://vercel.com/changelog/introducing-ai-elements)
- [Vercel AI Elements GitHub](https://github.com/vercel/ai-elements)
- [CopilotKit CoAgents + LangGraph](https://www.copilotkit.ai/blog/build-full-stack-apps-with-langgraph-and-copilotkit)
- [Chainlit HITL open issue](https://github.com/Chainlit/chainlit/issues/1998)
- [Mantine vs shadcn/ui 2026](https://saasindie.com/blog/mantine-vs-shadcn-ui-comparison)
- [shadcn vs MUI vs Ant Design 2026](https://adminlte.io/blog/shadcn-ui-vs-mui-vs-ant-design/)
- [Next.js + FastAPI JWT httpOnly cookie guide](https://medium.com/@sl_mar/building-a-secure-jwt-authentication-system-with-fastapi-and-next-js-301e749baec2)
- [Onyx enterprise RAG platform](https://github.com/onyx-dot-app/onyx)
- [LangGraph HITL + FastAPI + React demo](https://github.com/esurovtsev/langgraph-hitl-fastapi-demo)
