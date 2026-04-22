# Research: RAG Evaluation Frameworks & LLM Provider Strategy

*Research date: April 2026*

**Current stack**: LangGraph 0.6, FastAPI, Qdrant, PostgresSaver, 3-layer guardrails, RBAC, 14-node graph, HITL via interrupt. Running GPT-4o-mini + Groq Llama 3.3 70B.

**Current eval state**: `tests/evaluation/` has RAGAS wired up (61 Q&A pairs, runner, thresholds, CI gate). `eval_results/` directory **exists but is empty** — RAGAS has never actually been run against the full pipeline with results captured.

---

## Part A: RAG Evaluation Framework Landscape

### Framework comparison

| Framework | Type | Core metrics | Online eval | Offline eval | CI/CD | Pricing (2026) | LangGraph fit |
|---|---|---|---|---|---|---|---|
| **RAGAS 0.4.3** | OSS library | Faithfulness, Answer Relevancy, Context Precision/Recall, Noise Sensitivity, G-Eval, synthetic test gen | No | ✓ native | pytest | Free | Already integrated; no dashboard |
| **LangSmith** | SaaS (LangChain) | LLM-as-judge, RAGAS-style, custom evaluators, pairwise, regression experiments | ✓ sampled prod traces | ✓ datasets | ✓ | Dev free (5k traces, 14d); Plus $39/seat + $2.50/1k traces; Enterprise custom | Best-in-class — zero-instrumentation |
| **TruLens** | OSS library | RAG Triad (Context Relevance, Groundedness, Answer Relevance) + OTel | ✓ (OTel) | ✓ | ✓ | Free; Snowflake paid tier | Medium — requires TruSession wrapper |
| **DeepEval** | OSS + SaaS (Confident AI) | 50+ metrics (G-Eval, hallucination, task completion, bias, toxicity, red-team) | ✓ via Confident AI | ✓ | pytest-native | OSS free; Confident AI $19.99/seat + $1/GB-month | OSS = Task Completion only; Confident AI = deeper LangGraph tracing |
| **Arize Phoenix** | OSS (OTel) | RAG relevance, hallucination, QA correctness, LLM-as-judge, clustering/anomaly detection | ✓ OTel traces | ✓ | ✓ | Fully free self-hosted, no feature gating | Native LangGraph via OpenInference |
| **Giskard v3** | OSS + SaaS Hub | RAGET auto-gen tests, 40+ red-team probes, hallucination, compliance scans | Scheduled (Hub) | ✓ | ✓ | OSS free; Hub enterprise; EU-native GDPR/SOC2 | Medium — framework-agnostic SDK |
| **Langfuse** | OSS + Cloud | RAGAS integration, custom LLM-as-judge, dataset experiments, prompt mgmt, cost tracking | ✓ | ✓ | ✓ | Self-hosted free; Cloud free tier + usage-based; Enterprise for SCIM/audit | Strong — OTel + `@observe` decorator |

### Online (production) vs Offline (dataset) coverage

| Concern | Best tool | Why |
|---|---|---|
| Trace every prod request + SSE + HITL interrupts | **LangSmith or Langfuse** | Both auto-trace LangGraph nodes, capture `interrupt()`, track Postgres checkpointer |
| Sampled LLM-as-judge on live traffic | LangSmith / Langfuse / Phoenix | All support sampled online evaluators |
| Offline regression on your 61-pair set | **RAGAS** (already in place) | No changes needed |
| Pre-release red-team (injection, PII leak, jailbreak) | **Giskard RAGET + DeepEval** | Complements your 3-layer guardrails with adversarial coverage |
| RBAC leak testing (can `analyst` retrieve `c_level`?) | Custom pytest + Giskard probes | Neither tool does this natively — need role-matrix test harness |

### Recommended eval stack for THIS project

| Layer | Tool | Why |
|---|---|---|
| Offline regression (keep) | RAGAS 0.4.3 | Already gates PRs |
| Production tracing + online eval | **Langfuse (self-hosted)** | You already have Postgres + Docker. Self-hosting keeps financial query traces inside your VPC — a hard requirement for RBAC-sensitive data |
| Red-team / adversarial | Giskard OSS + DeepEval | Weekly CI run against guardrails node; fail build on new jailbreaks |
| HITL approval analytics | Langfuse custom views | Annotate interrupted traces with approve/reject to tune the HITL threshold |

**Why not LangSmith despite tight LangGraph synergy**: data residency. A financial-docs RAG with `c_level` confidentiality cannot route production traces to LangSmith Cloud without a BAA/DPA. LangSmith self-hosted is Enterprise-tier (undisclosed, typically $1k–5k+/mo). Langfuse self-hosted is free.

**Migration from current `LANGCHAIN_API_KEY=` setup**: ~4 hours. Langfuse exposes an OTel-compatible endpoint; swap env vars in `src/api/main.py` lifespan.

---

## Part B: LLM Provider & Model Selection (April 2026)

### Latest model pricing (per 1M tokens)

| Provider | Model | Model ID | Context | Input | Output | Notes |
|---|---|---|---|---|---|---|
| Anthropic | Opus 4.7 | `claude-opus-4-7` | 1M | $5.00 | $25.00 | New tokenizer (+~35% tokens vs 4.6). Top reasoning. |
| Anthropic | Sonnet 4.6 | `claude-sonnet-4-6` | 1M | $3.00 | $15.00 | 79.6% SWE-bench; strong injection resistance |
| Anthropic | Haiku 4.5 | `claude-haiku-4-5-20251001` | 200k | $1.00 | $5.00 | Fast/cheap; classification, routing |
| OpenAI | GPT-5.4 | `gpt-5.4` | 270k | $2.50 | $15.00 | Flagship reasoning |
| OpenAI | GPT-5.4 Mini | `gpt-5.4-mini` | 270k | $0.75 | $4.50 | Mid-tier workhorse |
| OpenAI | GPT-5.4 Nano | `gpt-5.4-nano` | 270k | $0.20 | $1.25 | Cheapest capable |
| OpenAI | GPT-4o-mini (legacy) | `gpt-4o-mini-2024-07-18` | 128k | $0.15 | $0.60 | **Your current generator** |
| Groq | Llama 3.3 70B | `llama-3.3-70b-versatile` | 128k | $0.59 | $0.79 | **Your current router/grader** — ultra-fast |
| Groq | Llama 4 Maverick | `llama-4-maverick` | ~1M | varies | varies | Multimodal, GPT-4o-class; 500 req/day free |
| Groq | Llama 4 Scout | `llama-4-scout` | 10M | varies | varies | Ultra-long context |
| Together | DeepSeek/Llama/Mistral catalog | many | varies | ~$0.20 | ~$0.60 | 100+ OSS models, cheapest flagship hosts |
| Fireworks | Llama 4 / DeepSeek R1 | many | varies | from $0.10 | — | Fast serverless, 10 RPM free |
| Mistral | Nemo / Large | `mistral-nemo`, `mistral-large-latest` | 128k | $0.02–$2.00 | varies | Cheapest European-hosted |

### Recommended per-node models

| Node | Current | Recommended | Temp | top_p | max_tokens | Why |
|---|---|---|---|---|---|---|
| `rbac_gate` | (deterministic) | No LLM | — | — | — | Keep |
| `guardrails` L1 (regex) | regex | regex | — | — | — | Keep |
| `guardrails` L2 (LLM Guard) | deberta | deberta | — | — | — | Keep |
| `guardrails` L3 (classifier) | Groq Llama 3.3 70B | **Haiku 4.5** | 0.0 | 1.0 | 50 | Sharper JSON/boolean output; Groq as fallback |
| `router` | Groq Llama 3.3 70B | **Keep Groq** (primary), Haiku 4.5 fallback | 0.0 | 1.0 | 32 | Groq wins on latency (~200ms vs ~800ms Haiku) |
| `query_rewriter` | Groq Llama 3.3 70B | **Sonnet 4.6** | 0.3 | 0.95 | 256 | Rewriting quality drives retrieval recall |
| `guardrails` contextualizer | Groq Llama 3.3 70B | **Keep Groq** | 0.0 | 1.0 | 128 | Latency-sensitive; runs every turn |
| `grader` | Groq Llama 3.3 70B | **Keep Groq**, Haiku 4.5 fallback | 0.0 | 1.0 | 16 | Binary classification, latency-sensitive in retry loop |
| `generator` | GPT-4o-mini | **Sonnet 4.6** | 0.2 | 0.9 | 1024 | Biggest quality lever; 1M context handles large multi-doc answers |
| `hallucination_checker` | GPT-4o-mini | **Sonnet 4.6** default, **Opus 4.7** when amount ≥ $100k | 0.0 | 1.0 | 256 | Two-tier: Sonnet everyday, Opus before HITL |
| `hitl_gate` | (deterministic) | No LLM | — | — | — | Keep |
| `response_formatter` | (deterministic) | No LLM | — | — | — | Keep |

### Cost impact — at 100k queries/month

Assumptions: avg 3k input / 600 output tokens per generator call; grader/router/guardrails ~1.3× per query (retries); hallucination ~1.1×; Opus on 5% (HITL path).

| Component | Current (monthly) | Proposed | Delta |
|---|---|---|---|
| Router + Grader + Guard L3 (Groq) | ~$15 | ~$15 | $0 |
| Query rewriter (Groq → Sonnet) | ~$2 | ~$25 | +$23 |
| Generator (GPT-4o-mini → Sonnet 4.6) | ~$81 | ~$1,800 | +$1,719 |
| Hallucination (GPT-4o-mini → Sonnet + Opus 5%) | ~$30 | ~$625 | +$595 |
| **Total** | **~$128** | **~$2,465** | **+$2,337 (~19×)** |

### Cost mitigation (brings delta to ~3–4× instead of 19×)

1. **Prompt caching on Claude** — up to 90% savings on repeated system prompts + chunks. Your RAG is highly cacheable. Realistic 60–70% cost cut on generator.
2. **Batch API** — 50% off for eval runs, periodic re-ingestion, nightly RAGAS.
3. **Tiered routing** — Haiku 4.5 as default generator; escalate to Sonnet only when grader confidence < threshold.
4. **Groq Llama 4 Maverick** for generator — GPT-4o-class quality at Groq latency/price. Middle path.

### Recommended config

```python
# src/services/llm_factory.py defaults (hybrid Groq + Claude strategy)
ROUTER        = {"provider": "groq",      "model": "llama-3.3-70b-versatile",   "temperature": 0.0, "max_tokens": 32}
GRADER        = {"provider": "groq",      "model": "llama-3.3-70b-versatile",   "temperature": 0.0, "max_tokens": 16}
CONTEXTUALIZER= {"provider": "groq",      "model": "llama-3.3-70b-versatile",   "temperature": 0.0, "max_tokens": 128}
GUARDRAILS_L3 = {"provider": "anthropic", "model": "claude-haiku-4-5-20251001", "temperature": 0.0, "max_tokens": 50}
REWRITER      = {"provider": "anthropic", "model": "claude-sonnet-4-6",         "temperature": 0.3, "top_p": 0.95, "max_tokens": 256}
GENERATOR     = {"provider": "anthropic", "model": "claude-sonnet-4-6",         "temperature": 0.2, "top_p": 0.90, "max_tokens": 1024}
HALLUCINATION = {"provider": "anthropic", "model": "claude-sonnet-4-6",         "temperature": 0.0, "max_tokens": 256}
HIGH_STAKES   = {"provider": "anthropic", "model": "claude-opus-4-7",           "temperature": 0.0, "max_tokens": 512}
```

### Final recommendation

- **Don't rip out Groq.** Its latency is unmatched for routing/grading inside retry loops. Keep as primary for `router`, `grader`, contextualizer.
- **Upgrade `generator` + `hallucination_checker` to Sonnet 4.6.** This is the user-facing quality surface.
- **Reserve Opus 4.7** for high-stakes validation (>$100k path) only.
- **Enable Anthropic prompt caching immediately** — 1-line `cache_control` addition cuts generator cost 60%+.
- **Migrate observability to self-hosted Langfuse** — non-negotiable for RBAC/financial data profile.

### Sources
- [RAGAS PyPI](https://pypi.org/project/ragas/)
- [LangSmith Plans and Pricing](https://www.langchain.com/pricing)
- [Langfuse RAG Observability and Evals](https://langfuse.com/blog/2025-10-28-rag-observability-and-evals)
- [Arize Phoenix GitHub](https://github.com/Arize-ai/phoenix)
- [DeepEval GitHub](https://github.com/confident-ai/deepeval)
- [TruLens RAG Triad](https://www.trulens.org/getting_started/core_concepts/rag_triad/)
- [Giskard AI Pricing & Products](https://www.giskard.ai/pricing)
- [Claude API Pricing: Haiku 4.5, Sonnet 4.6, Opus 4.7](https://benchlm.ai/blog/posts/claude-api-pricing)
- [OpenAI API Pricing 2026](https://nicolalazzari.ai/articles/openai-api-pricing-explained-2026)
- [Groq Pricing](https://groq.com/pricing)
