# Research: Production-Grade Hardening Roadmap

*Research date: April 2026*

What separates a "portfolio/demo" RAG system from a "production-grade enterprise" RAG system, covering 10 concerns with 2026 best practices.

---

## 1. Observability & Monitoring

**Production bar**: Per-trace attribution for every LLM call, tool call, and vector query — emitted in OpenTelemetry GenAI semantic conventions. SLOs on TTFT p90 < 2s and retrieval p95 < 500ms, cost dashboards per user/tenant, alerts on hallucination rate, token spend, error rates.

| Layer | Standard (2026) | Notes |
|---|---|---|
| Tracing (OSS) | **Langfuse** or **Arize Phoenix** | Phoenix easier to self-host (single Docker); Langfuse needs Clickhouse+Redis+S3 but is MIT + 19k stars |
| Tracing (LangChain-coupled) | **LangSmith** | What you have today |
| Enterprise telemetry | **Datadog LLM Observability** or **Arize AX** | Datadog natively consumes OTel GenAI semconv v1.37+ |
| Wire protocol | **OTel GenAI semconv** | Still "experimental" but de-facto standard |
| Cost tracking | LLM gateway (Portkey/LiteLLM) emitting per-tenant token metrics | Per-request cost attributed by `tenant_id`/`user_id` span attributes |

**Your gap**: LangSmith hooks exist. No OTel spans, no cost-per-user metric, no alerts, no structured JSON logs with trace IDs.

---

## 2. Security & Compliance

**Production bar**: SOC 2 Type II is mandatory — 83–85% of enterprise buyers require it as a vendor prerequisite. For financial data add PCI DSS. Defense-in-depth for prompt injection (direct AND indirect — indirect injection via retrieved documents is now the #1 agent-attack vector of 2026). PII redaction both in-flight (Presidio ✓) AND at rest (embeddings themselves can leak PII via inversion — OWASP AISVS C08).

| Concern | Standard (2026) |
|---|---|
| Prompt injection (managed) | **Lakera Guard** (<50ms, learns from 100k daily attacks) |
| Prompt injection (self-hosted) | **Rebuff** (heuristics + LLM + vector-DB signature cache + canary tokens) |
| PII | Presidio (in-flight) + Lakera/Portkey PII scrubber on outputs too |
| Secrets | **AWS Secrets Manager** (native rotation) or **HashiCorp Vault**. Rotate every 30–90 days |
| Encryption at rest | Qdrant storage encryption + PostgreSQL TDE (AWS RDS KMS or `pgcrypto`) |
| Rate limiting | **Token-bucket for tokens**, not requests — 10k-token and 50-token prompts differ 200× in cost |
| Compliance frameworks | SOC 2 + ISO 27001 + GDPR + EU AI Act (high-risk provisions live Aug 2026) |

**Your gap**: JWT_SECRET in `.env`, guardrails cover direct injection but not indirect (from retrieved docs), no token-based rate limiting, no output-side PII scrubbing, no encryption-at-rest docs.

---

## 3. Scale & Performance

**Production bar**: Hybrid search is table stakes — pure dense retrieval is now a demo pattern. Recall@50 > 90% from hybrid, then cross-encoder rerank to top-5. Semantic cache cuts LLM cost 30–70% on RAG workloads (published production data: 20–45% hit rate).

| Layer | Standard (2026) |
|---|---|
| Vector DB (<10M chunks) | **Qdrant** (your choice) — 4ms p50, best-in-class |
| Vector DB (>50M vectors) | **pgvectorscale** (471 QPS vs Qdrant 41 QPS at 99% recall, 50M vectors) or Pinecone Serverless |
| Hybrid retrieval | BM25 (Qdrant 1.10+ native sparse vectors) + dense + Reciprocal Rank Fusion |
| Reranker | **Cohere Rerank 3.5** or **BGE-reranker-v2-m3** self-hosted. Standard: top-50 hybrid → rerank → top-5 |
| Semantic cache | **Redis LangCache** with 0.85–0.95 cosine threshold |
| Checkpointer scaling | `psycopg_pool.ConnectionPool` (max_size=10). **Watch TOAST bloat** — use "Pointer State Pattern" (store chunks in S3, reference in checkpoint) to reduce checkpoint size by up to 99.8% |
| Queueing | Celery (sync), Temporal (durable workflows), SQS (AWS fan-out) |

**Your gap**: Pure dense, no reranker, no cache, no sparse index, PostgresSaver stores full state (heavy RAG chunks) on every checkpoint.

---

## 4. Data Pipeline

**Production bar**: Incremental sync, not nightly rebuild. Track per-document `last_modified`, maintain a manifest, upsert only deltas. For SharePoint use Microsoft Graph's delta endpoint; for S3 use event notifications → SQS → ingestion Lambda. Landing zone (S3) decouples source from index. Document versioning so policy-change queries can be time-bounded.

- **Unstructured.io** or Docling (you have) for multimodal with table preservation
- **Apache Airflow / Dagster / Prefect** for orchestration
- **Deltalake** or S3 landing zone with manifest file tracking `doc_hash, last_synced, version`
- Chunk-quality review: LLM-as-judge score per chunk, reject below threshold, route to human review queue

**Your gap**: One-shot CLI script. No CDC, no versioning, no delta sync, no source-of-truth manifest.

---

## 5. Auth & User Management

**Production bar**: JWT with hardcoded dev users is fine for a demo, unacceptable for enterprise. 2026 pattern: OIDC for your app, with SAML enterprise connections per customer tenant (each tenant configures their own Okta/Entra/Ping). MFA required. Session revocation via token blocklist. Dynamic RBAC (admin UI) instead of code-defined roles.

| Role | Standard (2026) |
|---|---|
| CIAM (full stack) | **Auth0** or **Clerk** |
| Overlay SSO (keep your auth, add SAML) | **WorkOS** or **SSOJet** — cheapest if JWT stays |
| Enterprise IdP | Okta, Microsoft Entra ID, Ping Identity (SAML 2.0); OIDC for modern apps |
| AWS-native | **Cognito** + external IdP federation |
| Audit log | Immutable append-only (AWS QLDB, or Postgres `audit_log` with trigger + S3 export) |

**Your gap**: Dev passwords hardcoded, no SSO, no MFA, no audit log, roles are static Python config.

---

## 6. Deployment & Infra

| Concern | Recommendation (2026) |
|---|---|
| ECS Fargate vs EKS | **Fargate < ~15 containers, EKS above.** Fargate carries a 20–30% premium over EC2; at 50+ services EKS is 6–9× cheaper. **Your scale is Fargate territory.** |
| IaC | **Terraform** or **AWS CDK** (not raw CloudFormation) |
| Deploy strategy | Canary 5% → 25% → 100% with automated rollback on eval-score drop. Prompts and node code deploy via **feature flags**, not re-deploys |
| Secrets | AWS Secrets Manager with rotation Lambdas |
| Migrations | **Alembic** for Postgres (1.18.4 supports SQLAlchemy 2.0). Zero-downtime = expand-contract |
| Qdrant DR | Native **snapshots** to S3 (Qdrant 1.x built-in), RPO ≤ 1h |
| Multi-region | Active-active via Qdrant distributed mode + Aurora Global DB |

**Your gap**: ECS reference workflow only, no Terraform, no Alembic, no Qdrant snapshot schedule, no vault.

---

## 7. LLM-Specific Production Concerns

**Production bar**: Provider fallback is not optional — OpenAI and Anthropic both have hours-long outages quarterly. Gateway tier mandatory. Output caching + prompt versioning + eval-gated rollouts. Token budgets with circuit breakers that trip at 85% of provider TPM.

| Concern | Standard (2026) |
|---|---|
| Gateway | **Portkey** (SaaS) or **LiteLLM** (self-hosted) |
| Fallback | Gateway config: primary=Claude → GPT-4o → Llama 3.3 70B self-hosted |
| Circuit breaker | Trip on: TPM > 85%, P95 > 3× baseline, 5xx rate > 2%, cost budget breach |
| Prompt versioning | Langfuse Prompts, LangSmith Hub, or **LaunchDarkly AI Prompt Flags** |
| Degraded mode | Skip generation, return raw top-3 citations with disclaimer |
| Backpressure | Token-bucket queue with priority lanes (interactive > batch) |

**Your gap**: Groq→OpenAI fallback in `LLMFactory` but no gateway, no circuit breaker, no budget, no degraded-mode path.

---

## 8. Developer Experience

**Production bar**: Prompts and graph topology are configuration, not code. Non-engineers (PMs, domain experts) edit prompts in a playground and ship via flag flip. Every prompt change runs RAGAS automatically before merge.

- **LaunchDarkly Prompt Flags** — processed 45T evaluations/day in early 2026; built-in online evals for accuracy/relevance/toxicity
- **Langfuse Prompts** — free, self-hosted, versioned, A/B
- **Evaluation-driven dev**: PR → RAGAS diff vs baseline → block merge if faithfulness drops > 0.02

**Your gap**: Prompts in `src/config/prompts.py` (code deploys needed), no playground, eval only runs on `src/graph/**` touches.

---

## 9. UX & Enterprise Features

| Feature | Demo | Production |
|---|---|---|
| Conversation history | In-memory | Persistent, search, export, pin |
| Citations | Inline filename | **Clickthrough to PDF page with highlight** |
| Feedback | None | Thumbs + free-text, feeds eval dataset |
| Admin dashboard | None | Per-user cost, error rate, top failing queries, prompt version in use |
| Multi-tenant | Single DB | **Silo/Pool/Bridge** pattern — Silo (separate Qdrant collection per tenant) for strict isolation, Pool (shared index + metadata filter) for SMB. Your RBAC-in-Qdrant is already Pool-pattern |
| Export | None | PDF export, email-to-self |

**Your gap**: No citation clickthrough, no feedback capture, no admin dashboard, single-tenant.

---

## 10. Compliance & Governance

**Production bar**: GDPR right-to-be-forgotten means deleting user data from: (1) source, (2) chunks, (3) embeddings in Qdrant, (4) Postgres chat history, (5) checkpoints, (6) LangSmith/Langfuse traces, (7) backups with TTL. OWASP AISVS C08 governs this.

- **Qdrant**: point-delete by `user_id` payload filter; schedule tombstone compaction
- **Retention**: Postgres `pg_cron` scheduled purge; LangSmith TTL per project; S3 lifecycle rules
- **Model card**: generated from config (prompts + model IDs + eval scores), committed per release
- **Audit trail**: immutable `query_log` table (who, when, role, query, retrieved doc IDs, answer, confidence)

**Your gap**: No delete API, no retention policies, no model card, no audit log table.

---

# Prioritized Hardening Roadmap (ROI-Ordered)

Ordered by **impact ÷ effort** — do them top to bottom.

| # | Item | Why | Effort | Concrete action |
|---|---|---|---|---|
| **1** | **Hybrid search + Cohere/BGE reranker** | Single biggest quality jump; MRR +9pts, moves RAGAS faithfulness | 2–3 days | Add Qdrant sparse vectors (BM25), RRF merge top-50, Cohere Rerank 3.5 to top-5. Insert between `retrieval` and `grader` |
| **2** | **Secrets to AWS Secrets Manager + rotation** | Blocks SOC 2; JWT_SECRET validator is a band-aid | 1 day | Replace `.env` reads with `boto3.client('secretsmanager')`; 90-day rotation Lambda |
| **3** | **LLM gateway (Portkey/LiteLLM) with fallback + cache + budget** | Replaces custom LLMFactory; adds semantic cache (30–70% cost cut), per-user budgets, circuit breaker | 2 days | Drop-in proxy in front of OpenAI/Groq/Anthropic; Redis semantic cache (0.92 threshold) |
| **4** | **Immutable audit log + `tenant_id` in state** | Required for SOC 2 + GDPR + multi-tenant path | 1 day | New `audit_log` table (append-only trigger); add `tenant_id` to `RAGState` |
| **5** | **OTel GenAI semconv + per-user cost metrics** | Vendor-neutral observability; cost-per-user is the #1 question from pilots | 2 days | `opentelemetry-instrumentation-langchain` + Datadog/Phoenix exporter |
| **6** | **GDPR delete endpoint + retention TTLs** | One-line blocker on enterprise contracts | 2 days | `/users/{id}/purge` — Qdrant filter-delete, pg_cron purge > 90d, LangSmith TTL |
| **7** | **Indirect prompt injection defense (scan retrieved docs)** | Your 3-layer only scans user input. 2026 attacks embed malicious instructions in PDFs — this is the primary agent attack vector now | 2 days | Lakera Guard or Rebuff on retrieved chunks before the generator prompt |
| **8** | **Alembic migrations + Qdrant snapshot schedule + RPO/RTO docs** | Fails any enterprise security questionnaire without DR plan | 1 day | `alembic init`, baseline schema, nightly Qdrant snapshot to S3 with 30-day retention |
| **9** | **Prompt versioning + flag-driven rollout (Langfuse Prompts)** | Decouples prompts from deploys, enables canary | 2 days | Move prompts.py into Langfuse; load by name+version at graph-build |
| **10** | **WorkOS or Auth0 SSO + MFA** | Largest unlock for enterprise sales | 3 days | Replace dev users with WorkOS AuthKit; MFA; session revocation via token-version claim |

### Deliberately below the line (good, but not top-10 ROI)
- Multi-region active-active — wait for first real customer
- EKS migration — you're 14 containers, Fargate is correct
- Silo multi-tenancy — Pool pattern fine until > 50 tenants
- Differential privacy on embeddings — academic, not required for SOC 2

### What this buys you

**Items 1–3** move quality and cost-per-query materially. **Items 4–6** unblock enterprise security review. **Items 7–10** close 2026-specific gaps. Total: ~3 engineer-weeks if sequenced. Takes the project from "strong portfolio demo" to a system that could **pass a mid-market financial-services security review**.

### Sources
- [RAG at Scale: Production AI Systems in 2026 (Redis)](https://redis.io/blog/rag-at-scale/)
- [OpenTelemetry GenAI Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
- [Datadog LLM Observability + OTel GenAI](https://www.datadoghq.com/blog/llm-otel-semantic-convention/)
- [Vector DB benchmark 2026 (14 cases)](https://imranzaman-5202.medium.com/pgvector-vs-elasticsearch-vs-qdrant-vs-pinecone-vs-weaviate-a-14-case-benchmark-59add8eb9134)
- [Reranker Guide 2026 (ZeroEntropy)](https://zeroentropy.dev/articles/ultimate-guide-to-choosing-the-best-reranking-model-in-2025/)
- [Indirect Prompt Injection (Lakera)](https://www.lakera.ai/blog/indirect-prompt-injection)
- [LangGraph Postgres Checkpoint Bloat](https://azguards.com/distributed-systems/the-checkpoint-bloat-mitigating-write-amplification-in-langgraph-postgres-savers/)
- [Enterprise SSO 2026 (WorkOS vs Auth0 vs Okta)](https://securityboulevard.com/2026/03/enterprise-sso-platforms-compared-ssojet-vs-auth0-vs-workos-vs-okta-for-saas/)
- [ECS Fargate vs Kubernetes Cost 2026](https://dev.to/inboryn_99399f96579fcd705/cost-optimization-why-ecs-fargate-costs-3x-more-than-kubernetes-2026-reality-check-1h62)
- [Portkey vs LiteLLM vs OpenRouter 2026](https://www.pkgpulse.com/blog/portkey-vs-litellm-vs-openrouter-llm-gateway-2026)
- [OWASP AISVS C08 Memory/Embeddings/Vector DB](https://github.com/OWASP/AISVS/blob/main/1.0/en/0x10-C08-Memory-Embeddings-and-Vector-Database.md)
- [Multi-Tenant RAG 2026 (Silo/Pool/Bridge)](https://www.ijetcsit.org/index.php/ijetcsit/article/view/551)
- [SOC 2 Compliance 2026 (Sprinto)](https://sprinto.com/blog/soc-2-requirements/)
- [Token-Based Rate Limiting for AI Agents 2026 (Zuplo)](https://zuplo.com/learning-center/token-based-rate-limiting-ai-agents)
- [LaunchDarkly AI Prompt Flags](https://intellyx.com/2026/04/03/launchdarkly-extending-runtime-control-platform-to-ai-applications/)
- [Zero-Downtime Alembic Migrations](https://goldlapel.com/grounds/replication-scaling-cloud/alembic-zero-downtime-migrations)
