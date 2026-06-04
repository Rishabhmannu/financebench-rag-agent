# FinanceBench RAG Agent

[![PyPI](https://img.shields.io/pypi/v/financebench-rag-agent.svg)](https://pypi.org/project/financebench-rag-agent/)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![LangGraph 0.6](https://img.shields.io/badge/LangGraph-0.6-green.svg)](https://github.com/langchain-ai/langgraph)
[![Tests](https://img.shields.io/badge/tests-342%20passing-brightgreen.svg)]()
[![FinanceBench](https://img.shields.io/badge/FinanceBench-72.7%25%20pass-blue.svg)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/Rishabhmannu/financebench-rag-agent/blob/main/LICENSE)

A multi-agent RAG system for role-based access-controlled financial document Q&A. Achieves **72.7% correctness pass rate** on the public FinanceBench benchmark using selective agentic retrieval, a BGE cross-encoder reranker, and a self-hosted LLM observability stack.

## Evaluation results

Evaluated on the FinanceBench benchmark (150 questions across 32 companies):

| Metric | Value |
|---|---|
| Correctness pass rate | **72.7%** (109/150) |
| Refusal rate | 6.7% (10/150) |
| RAGAS faithfulness | 0.747 |
| DeepEval faithfulness | 0.844 |
| DeepEval contextual recall | 0.768 |

Per-slice pass rate: **lookup 68.6%** (n=86), **multi-hop 84.6%** (n=13), **calc 76.5%** (n=51).

The correctness judge is a Claude Sonnet 4.6 + structured-prompt setup calibrated to Cohen's κ = 0.932 against an 89-question hand-labeled set with an adversarial leniency guard. Full methodology, per-judge scores, and reproduction commands in [docs/evaluation.md](https://github.com/Rishabhmannu/financebench-rag-agent/blob/main/docs/evaluation.md).

## Comparison with published systems on FinanceBench

| System | Approach | Accuracy |
|---|---|---:|
| [Mafin 2.5 / PageIndex](https://github.com/VectifyAI/Mafin2.5-FinanceBench) | Vectorless reasoning over hierarchical document tree | **98.7%** |
| [DANA](https://arxiv.org/abs/2410.02823) | Domain-aware neurosymbolic agent with deterministic operators | 94.3% |
| GPT-4-Turbo · long context (128k) | Whole-document prompting | ~79% |
| Claude-2 · long context (100k) | Whole-document prompting | ~76% |
| **This project** | Multi-agent RAG with selective research-agent subgraph + RBAC + HITL | **72.7%** |
| [FinanceBench paper](https://arxiv.org/abs/2311.11944) baselines | Vector retrieval + GPT-4 / Llama-2 | 38–43% |
| GPT-4-Turbo · top-k vector RAG | Standard retrieval, no agent | ~19% |

Long-context approaches score higher but are not enterprise-deployable — 10-K filings frequently exceed 128k tokens, and whole-document prompting is impractical at scale due to latency and cost. The 72.7% here is measured on a production-shaped pipeline (fixed institutional corpus, batched retrieval, RBAC at the storage layer, HITL on high-stakes outputs).

## Demos

### Role-based access control
An analyst is refused a confidential invoice; a c-level re-login unlocks the same query — access enforced at the retrieval layer.

![RBAC role-switch demo](https://raw.githubusercontent.com/Rishabhmannu/financebench-rag-agent/main/docs/demos/rbac.gif)

### Human-in-the-loop approval
Finance is blocked at the $100K gate; an admin approves in a second terminal and the answer is released back — multi-party, across sessions.

![HITL multi-party approval demo](https://raw.githubusercontent.com/Rishabhmannu/financebench-rag-agent/main/docs/demos/hitl.gif)

### Conversation memory
Follow-up questions resolve against thread history — "And Microsoft?" is rewritten using the prior turn.

![Conversation memory demo](https://raw.githubusercontent.com/Rishabhmannu/financebench-rag-agent/main/docs/demos/memory.gif)

## Try it

```bash
pip install financebench-rag-agent
financebench setup                    # brings up the 4-service docker stack, seeds 8 sample PDFs
financebench login -u analyst         # password analyst123
financebench chat
```

For the full 360-PDF FinanceBench corpus (skips ~$5-15 of Voyage embedding cost + ~30 min ingest):

```bash
financebench seed --from-hf cmpunkmannu/financebench-voyage-finance-2-embeddings
```

## Architecture

![Architecture: 18-node LangGraph pipeline with RBAC gate, guardrails cascade, simple vs research-agent routing, hallucination check, and HITL approval, backed by Qdrant, PostgreSQL, and Redis](https://raw.githubusercontent.com/Rishabhmannu/financebench-rag-agent/main/docs/diagrams/architecture.png)

A router classifies each query as a simple lookup or research-required. Simple lookups take the fast direct path (retrieval → BGE reranker → grader → Claude generator); research queries enter a multi-turn subgraph that decomposes the question, retrieves per sub-question, grades sufficiency, and synthesizes a final answer. RBAC is enforced at the Qdrant payload-filter level — agentic queries cannot bypass access control. High-stakes answers (above a per-role dollar threshold) pause via LangGraph's `interrupt()` for multi-party human approval, with state checkpointed to Postgres so the workflow survives container restarts.

## Tech stack

- **Backend** — FastAPI · LangGraph · Qdrant · PostgreSQL · Redis · PyJWT
- **Client** — `financebench` CLI: typer · rich · prompt_toolkit · httpx-sse · token-streaming over SSE
- **LLMs** — Claude Sonnet 4.6 · gpt-4o-mini · Llama 3.3 (via Groq, optional)
- **Retrieval** — OpenAI text-embedding-3-small or voyage-finance-2 · BGE-reranker-v2-m3 cross-encoder
- **Observability** — self-hosted LiteLLM proxy + Langfuse v3 + Redis semantic cache (full stack only)
- **Safety** — Microsoft Presidio PII detection · LLM Guard · LLM classifier (3-layer cascade)
- **Evaluation** — RAGAS · DeepEval · custom LLM correctness judge

## Known limitations

- **Not deployed to production** — runs locally via `docker compose up -d`. No public URL or live traffic.
- **CLI is the canonical client today.** A Next.js web frontend is in progress in `web/` but not wired into the deployment story.
- **Below the top-published systems** (Mafin 2.5 at 98.7%, DANA at 94.3%) — see comparison table above for context.

## Running from source

```bash
git clone https://github.com/Rishabhmannu/financebench-rag-agent.git
cd financebench-rag-agent
pip install -e ".[backend,dev]" && cp .env.example .env   # backend extras + dev tools
financebench setup                                         # docker compose + seed corpus
```

For self-hosting the full 11-service stack (LiteLLM + Langfuse), upgrade flows, and production hardening, see [docs/deploy.md](https://github.com/Rishabhmannu/financebench-rag-agent/blob/main/docs/deploy.md) and [docs/upgrade.md](https://github.com/Rishabhmannu/financebench-rag-agent/blob/main/docs/upgrade.md).

## Documentation

- [docs/cli.md](https://github.com/Rishabhmannu/financebench-rag-agent/blob/main/docs/cli.md) — CLI reference, slash commands, multi-party HITL workflow
- [docs/deploy.md](https://github.com/Rishabhmannu/financebench-rag-agent/blob/main/docs/deploy.md) — Self-host: stack profiles, env vars, backup, hardening
- [docs/upgrade.md](https://github.com/Rishabhmannu/financebench-rag-agent/blob/main/docs/upgrade.md) — Upgrade cookbook by change type
- [docs/evaluation.md](https://github.com/Rishabhmannu/financebench-rag-agent/blob/main/docs/evaluation.md) — Methodology, results, reproduction
- [docs/engineering-log.md](https://github.com/Rishabhmannu/financebench-rag-agent/blob/main/docs/engineering-log.md) — Engineering decisions and tradeoffs
- [docs/setup.md](https://github.com/Rishabhmannu/financebench-rag-agent/blob/main/docs/setup.md) — Test accounts, environment, dev commands
- [docs/architecture.md](https://github.com/Rishabhmannu/financebench-rag-agent/blob/main/docs/architecture.md) · [docs/api-reference.md](https://github.com/Rishabhmannu/financebench-rag-agent/blob/main/docs/api-reference.md) · [docs/rbac-matrix.md](https://github.com/Rishabhmannu/financebench-rag-agent/blob/main/docs/rbac-matrix.md) · [web/README.md](https://github.com/Rishabhmannu/financebench-rag-agent/blob/main/web/README.md)

## License

MIT
