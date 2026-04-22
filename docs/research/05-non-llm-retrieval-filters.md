# Non-LLM Retrieval Filters — Research

*Research date: 2026-04-22. Pre-Sprint-7.5 investigation.*

## Landscape

Three distinct design patterns for improving retrieval precision without adding LLM calls:

1. **Stronger retrieval at the source** — ColBERTv2, SPLADE (replaces or augments the bi-encoder).
2. **Post-retrieval learned filters** — small classifiers (logistic regression, XGBoost, LambdaMART, distilled MiniLM cross-encoders) that take reranker output + auxiliary features and produce keep/drop decisions.
3. **Deterministic / structural filters** — NER + entity linking, metadata overlap rules, BM25 score floors.

The space between cross-encoder reranker and LLM grader is **under-explored in modern RAG literature**. LambdaMART / XGBoost stacking is mature in classical IR (Elasticsearch LTR, Metarank) but rare in 2024-25 RAG papers. LTRR (Kim et al., SIGIR 2025) is the closest published work, and uses XGBoost at the retriever-routing layer, not post-retrieval.

## Technique-by-technique comparison

| # | Technique | What it does | Published numbers | Effort | Expected ctx_precision gain for our case |
|---|-----------|--------------|-------------------|--------|------------------------------------------|
| 1 | **ColBERT v2 / PLAID** | Late-interaction, per-token encoding, MaxSim scoring | Superlinked FinanceBench: ColBERTv2 + SentenceSplitter had ~10% MRR advantage over second-best | Medium — Qdrant native multivector support, but 10–50× storage | +0.05 to +0.10 |
| 2 | **SPLADE / uniCOIL** | BERT-MLM sparse vectors, captures synonyms BM25 misses | DistilSPLADE beats BM25 on BEIR zero-shot | Low — drop-in for existing BM25 slot | +0.02 to +0.05 (marginal vs existing BM25+dense) |
| 3 | **XGBoost/LightGBM LTR over features** | Combines dense score + BM25 + reranker + metadata flags into calibrated classifier | LTRR SIGIR 2025; Elasticsearch LTR standard practice | Low–Medium | **+0.05 to +0.15** with sufficient labels |
| 4 | **Chunk quality classifier at ingestion** | Predicts "will this chunk ever be useful" before indexing | ChunkRAG: 0.180 → 0.467 chunk relevance (but uses LLM, not small model) | Medium | +0.03 to +0.08 |
| 5 | **NER + entity-linking deterministic filter** | spaCy/FinBERT extracts tickers; require query-chunk entity overlap | FinBERT-MRC F1 92.78–96.80% on financial NER | **Low** — spaCy + SEC EDGAR ticker dictionary | **+0.05 to +0.20 specifically for cross-entity contamination** |
| 6 | **Distilled MiniLM from LLM labels** | Use Claude grader to label pairs, distill into MiniLM cross-encoder | TWOLAR, PairDistill 2024; MiniLM 33M params competitive with 560M BGE | Medium–High | +0.05 to +0.12, 10× faster than BGE |
| 7 | **LambdaMART stacking (LTR last-mile)** | Combines all signals into single calibrated ranker | NDCG@10 gains of 5–15% typical in commercial IR | Low–Medium | +0.05 to +0.10 |
| 8 | **CRAG-style T5 evaluator** | Fine-tune T5 to score (q, chunk) pairs | +26.7 pp accuracy vs vanilla RAG on PopQA | Medium–High | +0.05 to +0.15 |

## Critical insight from CRAG reproducibility study (arXiv 2603.16169)

> "The T5 retrieval evaluator primarily relies on named entity alignment rather than semantic similarity."

In other words — CRAG's expensive learned evaluator is, in effect, a **noisy neural NER-overlap classifier**. For financial 10-K RAG where cross-entity contamination is likely the dominant failure mode, a deterministic entity-overlap filter approximates what the learned evaluator is actually doing — at essentially zero cost.

This is the single most important finding for our pipeline.

## Novelty analysis

**Standard practice (not novel):**
- ColBERT v2 as a reranker (Qdrant/Weaviate tutorials, blog-grade content)
- SPLADE replacing BM25 (Qdrant cookbook)
- LLM chunk filtering (ChunkRAG, Self-RAG, CRAG — well covered)
- Metadata filtering on tickers (Bedrock/LangChain 1-liner APIs)
- Cross-encoder distillation (2024 is crowded: TWOLAR, PairDistill, LLMDistill4Ads)

**Genuinely underexplored for portfolio differentiation:**
- **LTR stacking layer that consumes signals your pipeline already produces** — dense cosine, BM25 score, BGE-reranker score, entity-overlap flag, section priors — and outputs calibrated keep/drop. LTRR is framed as retriever routing, not post-retrieval filtering. Training it on labels from your own LLM grader (self-distillation) would be a novel angle.
- **Deterministic entity-linking filter on SEC EDGAR's CIK/ticker universe** — explicitly doing what CRAG's expensive evaluator implicitly does. "Strip the neural network away and just do the thing" is a defensible contribution.
- **Ablation study** comparing: current pipeline, +entity-overlap rule, +XGBoost LTR stacker trained on grader labels, +distilled MiniLM, combined. Nobody has published this on 10-Ks with current (2025) components.

## Opinionated recommendation

**Day 1 — Deterministic entity-overlap filter (3-4 hours):**
1. spaCy `en_core_web_lg` + SEC EDGAR CIK/ticker dictionary (free, ~10k entries) to extract tickers from queries.
2. Tag each chunk with its set of mentioned tickers at ingestion (cheap — 10-Ks have the filer's ticker on every page).
3. After BGE-reranker: if query mentions any ticker and chunk mentions none, drop it.
4. Re-run RAGAS. Prediction: **context_precision 0.627 → 0.72–0.78**.

This is the single highest-leverage move because cross-entity contamination is likely the dominant failure mode in a multi-company 10-K corpus.

**Day 2-3 — LTR stacker (only if Day 1 didn't resolve it):**
1. Log features for every (query, chunk) pair: dense cosine, BM25, BGE-reranker score, entity-overlap flag, section tag, doc_type, chunk length.
2. Use existing Claude grader to label a few thousand pairs (already doing this; just persist the labels).
3. Train XGBoost binary classifier. Expect AUC 0.85–0.92.
4. Replace LLM grader for confident predictions (>0.9 or <0.1); fall through to LLM for borderline.
5. Re-run RAGAS, measure cost/latency reduction.

**Why this over alternatives:**
- ColBERT: big infra lift for likely +0.05 gain BGE partly captures.
- SPLADE: tiny gains on top of existing BM25+dense hybrid.
- Distilled MiniLM: right answer at scale, overkill for 49 chunks.
- Entity overlap + XGBoost stacker: specific to finance, exploits CRAG-reproduction insight, matches LTRR direction without duplication, produces clean measurable cost/precision/latency tradeoff table for portfolio writeup.

**Caveat:** magnitude of gain on our 61-question eval depends on how many failures are cross-entity contamination vs truly-ambiguous-within-company. Eyeball 20 failure cases from the current RAGAS run before committing.

## Sources

- [LTRR: Learning To Rank Retrievers for LLMs (SIGIR 2025)](https://arxiv.org/abs/2506.13743)
- [CRAG reproducibility study (2024)](https://arxiv.org/abs/2603.16169)
- [ChunkRAG (2024)](https://arxiv.org/abs/2410.19572)
- [FinBERT-MRC (arXiv 2205.15485)](https://ar5iv.labs.arxiv.org/html/2205.15485)
- [ColBERT on Qdrant](https://qdrant.tech/documentation/fastembed/fastembed-colbert/)
- [Modern Sparse Neural Retrieval — Qdrant](https://qdrant.tech/articles/modern-sparse-neural-retrieval/)
- [Elasticsearch Learning to Rank](https://www.elastic.co/search-labs/blog/elasticsearch-learning-to-rank-introduction)
- [XGBoost Learning to Rank](https://xgboost.readthedocs.io/en/latest/tutorials/learning_to_rank.html)
- [PairDistill (2024)](https://arxiv.org/html/2410.01383)
- [FinanceRAG winning entry](https://github.com/cv-lee/FinanceRAG)
- [Metadata-Driven RAG for Financial QA (arXiv 2510.24402)](https://arxiv.org/abs/2510.24402)
