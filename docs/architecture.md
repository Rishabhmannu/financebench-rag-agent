# Architecture

## Overview

The Enterprise RAG Agent is a `StateGraph` built with LangGraph 0.6. Every query flows through 14 nodes connected by 5 conditional edge routers. State is a `TypedDict` ([src/models/state.py](../src/models/state.py)) that accumulates across the pipeline; per-turn fields are reset on each new invocation, while `messages` is appended via the `add_messages` reducer to preserve conversation history.

## Graph Topology

```
START
  │
  ▼
rbac_gate ──► guardrails ─┬─► router ─┬─► retrieval ──► grader ─┬─► generator ──► hallucination_checker ─┬─► hitl_gate ─┬─► response_formatter ──► END
                          │           │                          │                                        │              │
                          │           ├─► clarification_response │─► query_rewriter                       │              ├─► blocked_response (rejected)
                          │           │         │                │         │                              │              │
                          │           │         └────► END       │         └────► (retry retrieval)       │              │
                          │           │                          │                                        │              │
                          │           └─► out_of_scope_response  └─► no_info_response                     └─► (retry     │
                          │                     │                            │                                generator) │
                          │                     └────► END                   └────► END                                  │
                          │                                                                                              │
                          └─► blocked_response ──► END
```

## Node Responsibilities

### Entry and Safety

| Node | File | Purpose |
|------|------|---------|
| `rbac_gate` | [src/graph/nodes/rbac_gate.py](../src/graph/nodes/rbac_gate.py) | Maps JWT role → allowed doc types/confidentiality |
| `guardrails` | [src/graph/nodes/guardrails.py](../src/graph/nodes/guardrails.py) | PII redaction (Presidio), 3-layer injection defense, query contextualization |

The guardrails node runs three injection checks in order of cost: (1) regex heuristics, (2) LLM Guard's PromptInjection scanner, (3) an LLM classifier — only invoked when LLM Guard returns a borderline score (0.5 ≤ x < 0.9). After safety checks pass, it runs a contextualization step that rewrites coreferential follow-ups (e.g., "what about Microsoft?") into standalone questions using prior message history.

### Routing

| Node | File | Purpose |
|------|------|---------|
| `router` | [src/graph/nodes/router.py](../src/graph/nodes/router.py) | Classifies intent: retrieval, clarification, or out_of_scope |

Structured output via Pydantic's `RouterDecision` schema. Uses Llama 3.3 70B on Groq (free tier) with automatic fallback to GPT-4o-mini.

### Retrieval and Correction

| Node | File | Purpose |
|------|------|---------|
| `retrieval` | [src/graph/nodes/retrieval.py](../src/graph/nodes/retrieval.py) | Qdrant semantic search with RBAC payload filter |
| `grader` | [src/graph/nodes/grader.py](../src/graph/nodes/grader.py) | LLM grades relevance of each retrieved chunk |
| `query_rewriter` | [src/graph/nodes/query_rewriter.py](../src/graph/nodes/query_rewriter.py) | Rewrites the query if grading failed |

The grader checks each top-k chunk for relevance (binary). If fewer than `GRADING_MIN_RELEVANT_CHUNKS` are relevant, the graph loops back via `query_rewriter` up to `MAX_RETRIEVAL_RETRIES` (default 2) times. After retries are exhausted, the graph terminates with `no_info_response`.

### Generation and Verification

| Node | File | Purpose |
|------|------|---------|
| `generator` | [src/graph/nodes/generator.py](../src/graph/nodes/generator.py) | GPT-4o-mini generates answer from relevant chunks; appends AIMessage to state |
| `hallucination_checker` | [src/graph/nodes/hallucination.py](../src/graph/nodes/hallucination.py) | LLM verifies answer is grounded in sources |

If the hallucination check returns `grounded=False` with confidence below `HALLUCINATION_THRESHOLD` (0.7), the graph loops back to `generator` up to `MAX_GENERATION_RETRIES` (default 2) times. After retries, the answer is returned with a disclaimer.

### Approval and Formatting

| Node | File | Purpose |
|------|------|---------|
| `hitl_gate` | [src/graph/nodes/hitl_gate.py](../src/graph/nodes/hitl_gate.py) | Interrupts graph if dollar amount exceeds role threshold |
| `response_formatter` | [src/graph/nodes/response_formatter.py](../src/graph/nodes/response_formatter.py) | Builds final response with deduplicated source list |

The HITL gate extracts dollar amounts from the generated answer via regex (handles `$100k`, `$2.5M`, `$383.3 billion`, etc.), compares against `requires_hitl_above` from [rbac_config.py](../src/config/rbac_config.py), and calls `interrupt()` when over threshold. Execution resumes via `POST /hitl/approve` or `/hitl/reject`, which invoke the graph with `Command(resume="approved"|"rejected")`.

### Terminal Nodes

All four terminal nodes live in [src/graph/nodes/terminal_nodes.py](../src/graph/nodes/terminal_nodes.py):

- `blocked_response` — guardrails or HITL rejection
- `out_of_scope_response` — router classified query as off-topic
- `clarification_response` — router needs more info
- `no_info_response` — retrieval+grading failed after retries

## Conditional Edge Logic

All routing logic is in [src/graph/edges.py](../src/graph/edges.py):

| Router | From | Targets |
|--------|------|---------|
| `route_after_guardrails` | `guardrails` | `clean` → `router`, `blocked` → `blocked_response` |
| `route_after_router` | `router` | `retrieval`, `clarification`, `out_of_scope` |
| `route_after_grading` | `grader` | `sufficient` → `generator`, `retry` → `query_rewriter`, `no_info` → `no_info_response` |
| `route_after_hallucination` | `hallucination_checker` | `grounded`/`disclaimer` → `hitl_gate`, `retry` → `generator` |
| `route_after_hitl` | `hitl_gate` | `no_approval_needed`/`approved` → `response_formatter`, `rejected` → `blocked_response` |

## State Management

`RAGState` ([src/models/state.py](../src/models/state.py)) has ~21 fields grouped by concern:

- **Input**: `messages` (with `add_messages` reducer)
- **Auth**: `user_id`, `user_role`, `allowed_doc_types`
- **Guardrails**: `guardrail_status`, `detected_pii_entities`, `sanitized_query`
- **Routing**: `query_intent`
- **Retrieval**: `retrieved_chunks`, `retrieval_query`
- **Grading**: `relevant_chunks`, `grading_results`
- **Generation**: `generated_answer`
- **Hallucination**: `hallucination_status`, `hallucination_score`
- **HITL**: `requires_human_approval`, `human_decision`
- **Control flow**: `retrieval_retry_count`, `generation_retry_count`
- **Output**: `final_response`, `response_metadata`

The `messages` field uses LangGraph's `add_messages` reducer, which *appends* rather than replaces. This means when the same `thread_id` is re-invoked, prior messages are retained automatically (via PostgresSaver checkpointing), enabling multi-turn conversations.

## LLM Strategy

Managed by [LLMFactory](../src/services/llm_factory.py). Provider selection per node:

| Node | Primary | Fallback | Why |
|------|---------|----------|-----|
| `router` | Groq Llama 3.3 70B | OpenAI GPT-4o-mini | Classification task, free tier |
| `grader` | Groq Llama 3.3 70B | OpenAI GPT-4o-mini | Binary classification, high volume |
| `query_rewriter` | Groq Llama 3.3 70B | OpenAI GPT-4o-mini | Simple rewriting |
| `guardrails` (contextualizer, injection layer 3) | Groq Llama 3.3 70B | OpenAI GPT-4o-mini | Classification/rewriting |
| `generator` | OpenAI GPT-4o-mini | Groq Llama 3.3 70B | Financial accuracy matters |
| `hallucination_checker` | OpenAI GPT-4o-mini | Groq Llama 3.3 70B | Nuanced grounding assessment |

Providers fall back automatically on exceptions (rate limits, outages).

## Ingestion Pipeline

Separate from the query graph. Lives in [src/ingestion/](../src/ingestion/):

1. [docling_loader.py](../src/ingestion/docling_loader.py) — PDF → per-page text (pypdf) + markdown (Docling)
2. [metadata_extractor.py](../src/ingestion/metadata_extractor.py) — Detects `doc_type`, `company`, `confidentiality`
3. [chunker.py](../src/ingestion/chunker.py) — Recursive splitter (~800 chars, 150 overlap), chunks each page independently so every chunk carries a `page_number`
4. [qdrant_uploader.py](../src/ingestion/qdrant_uploader.py) — Embeds via OpenAI text-embedding-3-small (1536 dims), upserts to Qdrant in batches

Run via `python scripts/seed_qdrant.py --sample` or `python scripts/ingest_documents.py --input data/raw/`.

## Persistence

PostgreSQL stores LangGraph checkpoints for HITL resumption and conversation history. Initialized in [src/api/main.py](../src/api/main.py) lifespan via `AsyncPostgresSaver`. On API startup, the checkpointer tables are created with `CREATE INDEX CONCURRENTLY`, which requires an autocommit connection (handled separately from the runtime pool).

If PostgreSQL is unavailable, the app logs an error and continues with HITL disabled — the graph runs without a checkpointer and interrupts are auto-approved.

## Observability

LangSmith tracing is always on when `LANGCHAIN_API_KEY` is set. Every graph invocation is tagged with:
- `run_name`: `"rag_query"` or `"rag_query_stream"`
- `tags`: `["api", f"role:{user.role}"]`
- `metadata`: `{"user_id", "role", "thread_id", "hitl_enabled"}`

Project names are environment-specific via `settings.langchain_project_name` (e.g., `rag-agent-dev`, `rag-agent-prod`).

## Evaluation Baseline

Captured 2026-04-22 against the Sprint-6 pipeline (pure dense retrieval, GPT-4o-mini generator, Llama 3.3 on Groq for routing/grading). Source: [`tests/evaluation/eval_results/baseline_pre_optimization.json`](../tests/evaluation/eval_results/baseline_pre_optimization.json).

| Metric | Baseline | Interpretation |
|--------|----------|----------------|
| Faithfulness | 0.50 | ~50% of generated claims are backed by retrieved chunks. Low — partly from generator confabulation, partly from garbled sample PDFs. |
| Answer Relevancy | 0.68 | Responses often hedge ("I don't have enough information") when chunks are weak, pulling the average down. |
| Context Precision | 0.65 | Pure dense retrieval pulls relevant chunks but also noise. Reranker (Sprint 7a) should move this the most. |
| Context Recall | 0.67 | Retrieval is finding enough of the right chunks — the issue is ranking, not recall. |

Runtime: 15 min pipeline (61 questions × ~15s each), 1.5 min RAGAS scoring (4 metrics × 61 samples via `gpt-4o-mini` as evaluator).

**Diagnostic signal**: `context_recall (0.67) > context_precision (0.65)` is the textbook "finding right chunks, ranking poorly" pattern — exactly what cross-encoder reranking in Sprint 7a is designed to fix. Expect the biggest single metric jump there.

CI thresholds in [`tests/evaluation/eval_config.py`](../tests/evaluation/eval_config.py) are set at baseline + 0.02 so the gate catches regressions but doesn't require Sprint 7 to land. `TARGET_THRESHOLDS` in the same file captures the aspirational post-Sprint-7 values.
