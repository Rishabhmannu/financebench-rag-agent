# CLI Guide

The `financebench` CLI is the canonical client for this project. It hits the FastAPI `/v1/*` endpoints over HTTP and SSE, so it works against a local docker stack or any backend you point it at via `--base-url` on login.

## Install

```bash
pip install -e ".[cli]"          # from a local checkout
# or, when published:
pip install financebench-rag-cli
```

The script is registered as `financebench` (see `pyproject.toml` `[project.scripts]`).

## First-time setup

```bash
financebench setup
```

What it does:

1. Locates a repo checkout — uses the current directory if it looks like the project root, otherwise clones to `~/.financebench/repo`.
2. Prompts for API keys (`OPENAI_API_KEY` and `ANTHROPIC_API_KEY` are required; `VOYAGE_API_KEY` and `GROQ_API_KEY` are optional). Writes `.env` (or updates an existing one — Enter to keep the current value).
3. Brings up the 4-service minimal docker stack (`api`, `qdrant`, `postgres`, `redis`).
4. Waits up to ~6 min for `/v1/health` to return 200. First boot is the slowest step because the BGE reranker (~570MB) downloads from HuggingFace.
5. Pre-warms the reranker + sparse embedder via `GET /v1/warm` so the first chat doesn't pay that cost.
6. Seeds the sample corpus (8 PDFs from `data/sample/`) — ~60s and ~$0.001 in embedding cost.

For the full 11-service stack (LiteLLM + Langfuse observability), pass `financebench setup --full` instead.

## Per-terminal identity: `FB_PROFILE`

Credentials live at `~/.financebench/profiles/{profile}.json`, keyed on the `FB_PROFILE` env var. Different terminals can be logged in as different users without overwriting each other — essential for the multi-party HITL demo.

```bash
# Terminal 1: approver
export FB_PROFILE=admin
financebench login -u admin          # admin123

# Terminal 2: requester
export FB_PROFILE=finance
financebench login -u finance        # finance123
```

If you don't set `FB_PROFILE`, you're on the `default` profile — fine for single-terminal use.

## Test accounts (development only)

| username | password | role | what they see |
|---|---|---|---|
| analyst | analyst123 | analyst | 10-K filings, public confidentiality only |
| finance | finance123 | finance | 10-K + invoices + expense_policy, public + internal; HITL fires above $100K |
| hr | hr123 | hr | expense_policy only |
| clevel | clevel123 | c_level | all doc types incl. confidential; HITL above $1M |
| admin | admin123 | admin | full access; never triggers HITL; approves everything |

See `src/api/routes/auth.py` (`DEV_USERS` dict) for the source of truth.

## Chat

```bash
financebench chat                              # streaming REPL (default)
financebench chat "What was Apple revenue?"    # one-shot streaming
financebench chat --no-stream "..."            # one-shot non-streaming (scripting)
financebench chat --thread-id <id>             # continue an existing thread
```

The REPL boot pre-warms the backend models, then drops you at `user@financebench>`. Token streaming starts within ~2-3s of each query; the full multi-agent answer takes 15-30s depending on whether the query goes through the research-agent subgraph.

### Slash commands inside the REPL

| Command | Effect |
|---|---|
| `/role <name>` | Re-login as another role (prompts for password). Resets thread. |
| `/permissions` | Show current role's RBAC access matrix (doc types, confidentiality, HITL threshold) |
| `/thread new` | Start a fresh thread (clean conversation memory) |
| `/thread show` | Current thread id, turn count, session cost |
| `/threads` | Arrow-key picker over your prior threads. Enter to switch. |
| `/approvals` | Open the interactive approver inbox (admin/clevel only). Empty for other roles. |
| `/cost [N]` | Tail the last N LLM-cost records (admin only) |
| `/audit [N]` | Tail the last N structured events from the latest run log (admin only) |
| `/clear` | Clear the screen |
| `/help` | This list |
| `/quit`, `/exit` | Leave the REPL |

Anything not starting with `/` is sent as a chat query.

### Conversation memory

If you ask a follow-up like `What about Microsoft?` or `And in 2022?` on the same thread, the agent pulls the last 3 (human, ai) pairs from Postgres, rewrites your query into a self-contained question via a cheap LLM call, and runs retrieval against that. Generator also sees the last 2 turns when synthesizing.

What persists: full conversation history per thread, stored in Postgres via LangGraph's checkpointer (`pg_data` docker volume). Survives container restarts and laptop reboots. Wiped only by deleting the volume or running `financebench threads delete <id>`.

What does NOT persist: cross-thread memory, cross-user memory, learned preferences. Each thread is its own universe.

## Approvals (multi-party HITL)

When a HITL-gated role (`finance` / `c_level`) submits a query that mentions a dollar amount above their threshold, the agent generates a draft answer, then HITL pauses the graph. The requester sees a "Pending approval" panel — **no draft answer is shown to them**. An authorized approver in a separate session must release it.

### Approver workflow

```bash
financebench approvals review        # interactive arrow-key TUI
financebench approvals list          # plain table (scripting)
financebench approvals show <id>     # full review payload
financebench approvals approve <id> [--reason "..."]
financebench approvals reject  <id> --reason "Required"
```

The `review` TUI:
- Arrow keys to navigate the pending list. Each row shows requester name, role, dept, age, amount, query preview.
- Enter to drill into a request. Shows the draft answer, confidence score, sources cited, retrieval-fallback warning if grounding was weak.
- Arrow keys + Enter on the action picker to Approve / Reject / Back.
- Reject requires a non-empty reason (input dialog re-prompts if blank). The requester sees the reason in their result.
- After action, the list refreshes; the resolved item drops off automatically.
- Esc / q exits.

### Approval hierarchy

| Approver role | Can approve requests from |
|---|---|
| analyst | (none) |
| hr | (none) |
| finance | (none — self-approval is never allowed) |
| c_level | finance, hr, analyst |
| admin | everyone |

Defined in `src/config/rbac_config.py` `CAN_APPROVE_FOR`. Codified in source (not the DB-driven `ROLE_PERMISSIONS`) because approval hierarchy is org-level policy, not a tenant-editable knob.

### Requester polling

When the requester's chat REPL hits a `pending_approval`, it auto-polls `GET /v1/chat/result/{thread_id}` every 3s for up to 5 min. When status flips to `approved` or `rejected`, the answer (or rejection message + reason) renders inline. Ctrl+C drops out cleanly; the pause stays alive in Postgres and can be resumed later via `--thread-id`.

## Threads

```bash
financebench threads list                    # your own threads
financebench threads show <thread_id>        # messages + interrupt state
financebench threads delete <thread_id> -y   # destructive
```

Admins can read/delete any thread. Other roles are ownership-gated (403 on cross-user access).

## Status & admin

```bash
financebench status
# Profile, backend URL, API version, models loaded, your thread count
```

Admin slashes inside the REPL:
- `/cost` — aggregated LLM cost from `cost_logs/cost_log.jsonl` (host-mounted from container)
- `/audit` — recent events from `logs/run_*.jsonl` (Sprint 7.19 structured event log)

## Tearing down + upgrading

```bash
financebench down                        # docker compose down, volumes preserved
financebench down --volumes              # ALSO removes volumes (destroys data)
financebench upgrade                     # git pull + rebuild api + restart, volumes preserved
financebench upgrade --force             # allow upgrade even with uncommitted local changes
```

`upgrade` refuses to run if the cloned repo has uncommitted changes (so you don't lose local edits). Pass `--force` to override.

## Capturing terminal sessions for debugging

Copy-pasting REPL output into a bug report is slow and lossy (the ANSI colours don't survive, the TUI screens get mangled). The `fb-record` wrapper solves that — it uses `script(1)` to capture the *entire* terminal session into a file you can share verbatim.

```bash
fb-record financebench chat                  # capture a chat REPL
fb-record financebench approvals review      # capture an approver flow
fb-record curl http://localhost:8000/v1/health   # capture anything, really
```

Logs land at `~/.financebench/cli_sessions/session_<timestamp>_<cmd>.log`. The wrapper handles the macOS vs Linux `script` flag differences for you. The capture ends when the wrapped command exits normally (or you Ctrl+D out of an interactive prompt).

Browse + read captured sessions via the `logs` subcommand:

```bash
financebench logs list                       # newest first, with sizes
financebench logs show latest                # raw — includes ANSI for re-rendering
financebench logs show latest --clean        # ANSI stripped — paste this into a bug report
financebench logs path latest                # absolute path, for `pbcopy <`, opening in VS Code, etc.
financebench logs show <id> --clean | pbcopy # one-step: latest clean log on the macOS clipboard
```

Add an alias to your shell rc to make it one keystroke:

```bash
# ~/.zshrc
alias fbrec='/Users/rishabh/Documents/Documents/Agentic-AI-Course/CampusX-LangGraph-Course/scripts/fb-record'
```

Why script(1) and not a pure-Python rich.record-mode capture: `prompt_toolkit` TUIs (approval inbox, thread picker) and `getpass` bypass rich entirely. A Python-side recorder would silently miss those — exactly the parts most worth capturing during HITL testing.

## Known limitations

- The arrow-key TUI requires a real terminal. Piped or captured contexts (e.g., `financebench approvals review | cat`) will fail because `prompt_toolkit` can't drive its event loop.
- `/admin/costs` (full Langfuse dashboard) requires the `--full` stack. The `/cost` slash falls back to reading the on-disk `cost_log.jsonl` in minimal mode — same data, plainer table.
- macOS Terminal.app has a ~250ms Esc disambiguation delay. The TUI accepts `q` and `Ctrl+C` as backup cancel keys.
- First boot after a `docker volume rm hf_cache` re-downloads the BGE reranker (~570MB). Subsequent boots are fast.
