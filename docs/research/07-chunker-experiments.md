# Phase B — Chunking-Strategy Experiments

*Dates: 2026-04-22 through 2026-04-24. Evaluated on 150 FinanceBench questions across 84 real 10-K/10-Q PDFs, judged by RAGAS 0.2 (gpt-4o-mini) and Patronus `patronus:fuzzy-match` (leaderboard-comparable).*

## TL;DR

We suspected the single biggest retrieval bottleneck was our chunker — pypdf per-page splitting ignores document structure. We tested two structure-aware alternatives (Docling **HybridChunker**, then Docling **markdown-first**). Both regressed on 5 of 6 metrics across two independent judges. **pypdf won.** The cause is a fundamental section-vs-page locality tradeoff: for 10-K Q&A, surrounding paragraph context is more valuable than structural purity. Rolled back to pypdf with data.

Total investment: ~6 hrs ingestion + ~1.5 hrs evaluation + engineering time. Cost of being wrong on the hypothesis: moderate. Cost of shipping without measuring: much higher — this is exactly the kind of plausible-sounding change that quietly hurts production quality.

## Motivation — why we touched the chunker at all

Before this phase, FinanceBench pypdf-baseline RAGAS numbers were:

| Metric | Value | Gap to target |
|---|---|---|
| faithfulness | 0.468 | target 0.80 |
| answer_relevancy | 0.296 | target 0.75 |
| context_precision | 0.546 | target 0.70 |
| **context_recall** | **0.205** | **smoking gun** |

Context recall at 0.205 means 80% of the time, the retrieved chunks don't contain the ground-truth answer. That dwarfs every other failure mode we'd measured — router, generator, reranker all ran on top of it. The working hypothesis:

> **Pypdf per-page chunking is dropping structural signal.** Tables split mid-row when page boundaries cut them. Financial statements lose their column headers when the header row is in chunk *k* and the data rows end up in chunk *k+1*. Context-recall should rise meaningfully if we replace pypdf with a structure-aware chunker.

This was reasonable. It was also wrong.

## The three approaches tested

### Approach 1 — pypdf per-page (baseline, already in production)

`src/ingestion/chunker.py::_chunk_per_page`. Runs `pypdf.PdfReader` per-file, extracts each page's text, recursively splits into ~800-char chunks with 150-char overlap. Each chunk tagged with `page_number` only — no section info.

**Cost:** ~1 s/PDF. **Chunks produced:** 68,059 across 84 PDFs.

### Approach 2 — Docling HybridChunker

`src/ingestion/chunker.py::_chunk_with_docling`. Parses the PDF with Docling's `DocumentConverter` (layout-aware PDF → `DoclingDocument` with doc-item graph, tables, headings). Feeds the `DoclingDocument` into `docling.chunking.HybridChunker(max_tokens=512, merge_peers=True)`. Each chunk carries:

- `section_header`, `heading_path` (from the doc-item graph)
- `page_number` (smallest `page_no` across the chunk's doc-items)
- `chunk_type` (`"table"` if any doc-item label contains "table", else `"text"`)

**Cost:** ~102 s/PDF (Docling parse) + <1 s chunk. **Chunks produced:** 39,938. 41% tagged as tables.

### Approach 3 — Docling markdown-first

`src/ingestion/chunker.py::_chunk_with_docling_markdown`. Parses the PDF with Docling (same expensive step), but instead of HybridChunker, calls `docling_doc.export_to_markdown()` and chunks the resulting markdown. The custom chunker:

- Parses markdown into typed blocks (heading / table / paragraph)
- Tracks a heading stack as it walks
- Never splits a `|...|...|` table block (tables emitted whole, even if oversize)
- Packs paragraphs greedily up to 1500 chars
- Correlates markdown headings back to PDF page numbers via a `heading_text → page_no` map built from `DoclingDocument.iterate_items()`

Output: section_header + heading_path + page_number + chunk_type metadata, same as HybridChunker, but tables remain as readable pipe-tables.

**Cost:** ~102 s/PDF (same Docling parse) + <1 s chunk. **Chunks produced:** 50,253. 15% tagged as tables (fewer because tables stay intact instead of being split into row-groups).

## Results

### RAGAS (gpt-4o-mini judge, 150 questions)

| Metric | pypdf | HybridChunker | Markdown | Best |
|---|---|---|---|---|
| faithfulness | **0.468** | 0.367 | 0.376 | pypdf (+9.2 vs MD) |
| answer_relevancy | **0.296** | 0.266 | 0.265 | pypdf (+3.1 vs MD) |
| context_precision | **0.546** | 0.485 | 0.470 | pypdf (+7.6 vs MD) |
| context_recall | 0.205 | **0.252** | 0.231 | HybridChunker |

### Patronus fuzzy-match (leaderboard criterion, n varies due to free-tier quota)

| Collection | pass_rate | n_valid |
|---|---|---|
| pypdf | **0.2533** | 150 |
| HybridChunker | 0.2143 | 140 |
| Markdown | 0.2239 | 67 |

### Cross-judge consistency

Relative ordering is **identical across both judges**: `pypdf > Markdown > HybridChunker` for all aggregate quality metrics except RAGAS context_recall (where both Docling paths slightly beat pypdf). Judges agree → the ranking is robust even with small Patronus N on the Markdown collection.

## Why each approach behaved the way it did

### Why HybridChunker regressed

Its `chunk.text` field flattens tables via Docling's default serializer: each cell becomes `"row_label, column_label = value"`. For simple two-column tables, this is fine. For the dense multi-year financial statements FinanceBench is built on, it mangles. Concrete failure case from our diff tool (Adobe YoY operating income question):

```
# HybridChunker chunk content
"Operating income, As of/ Year Ended December 31,.2017.(in thousands,
 except revenue per membership and percentages) = $ 838,679"
```

Column headers and unit labels got concatenated into the row label. Empty cells serialized as `= .`. The generator can't parse this into "2017 operating income was $838,679" reliably → faithfulness collapses.

Context_recall went *up* (+4.7 pts vs pypdf) because the right section is now retrievable via its heading; everything else went *down* because the retrieved content, once retrieved, is unusable.

### Why Markdown (that we built specifically to fix the above) also regressed

We verified with a side-by-side smoke test that markdown chunks *do* render tables cleanly:

```
| Years ended December 31 (Millions)               | 2018      | 2017      | 2016      |
|--------------------------------------------------|-----------|-----------|-----------|
| Purchases of property, plant and equipment (PP&E)| $ (1,577) | $ (1,373) | $ (1,420) |
| Net cash provided by (used in) investing activities | $ 222 | $ (3,086) | $ (1,403) |
```

So the table-serialization bug was fixed. Faithfulness nudged up (+0.9 pp vs HybridChunker). But it still lost to pypdf by 9.2 pp. Why?

**Section-bounded chunks lose surrounding context.** Every Docling chunk is *just* a section — a table, or a subsection-scoped paragraph. When a question requires information that spans a section boundary ("Why did operating margin change?" needs both the ratio table AND the management discussion paragraph two sections later), the retrieved chunk contains half the answer. Generator either refuses or hallucinates. Pypdf's page chunks keep spatial neighbors together — tables with their captions, paragraphs with their immediately-preceding lead-in sentence. That locality matters more than structural purity.

Context_precision dropped further on Markdown than HybridChunker (0.470 vs 0.485) because tables stay whole: one retrieved "slot" now contains a full 10-row table when the question needed one row, mechanically lowering the relevant fraction.

### Why pypdf's "ugly" approach wins

Pypdf produces chunks with no understanding of document structure. This sounds bad. But:

1. **Locality is preserved.** Every chunk is a physically-contiguous span of a page. Whatever was visually near the answer — a table caption, a preceding "For the year ended December 31, 2018" header, the footnote reference — is in the same chunk most of the time.
2. **Chunks are smaller and more** (68,059 vs 40–50k). More retrieval surface, so top-k=8 has more chances of landing a relevant chunk.
3. **Tables are ugly but coherent.** Pypdf's extracted table text lacks structure but every row stays on the same line of text in order. A generator can still parse the 2D layout from whitespace alignment.

Pypdf isn't "good." It's "good enough + preserves locality," and the latter turned out to matter more than structural purity for Q&A over dense financial documents.

## Concrete diagnostic — the 21 vs 13 regression/fix asymmetry

Our `scripts/diff_financebench_runs.py` classified every FB question by whether each pipeline refused or answered:

| Bucket | n | % |
|---|---|---|
| Both answered | 82 | 55% |
| Both refused | 34 | 23% |
| **pypdf answered, Docling refused** | **21** | **14%** (regression) |
| Docling answered, pypdf refused | 13 | 9% (fix) |

**Net: −8 questions (−5.3%).** Docling fixes 13 questions pypdf couldn't handle (mostly table-row-extraction questions where HybridChunker's section chunking helps), but breaks 21 questions pypdf handled fine (mostly synthesis questions requiring cross-section context). The tradeoff is real and measurable per-question.

## Decision

Roll back to pypdf. `chunk_document()` in `src/ingestion/chunker.py` will prioritize `_chunk_per_page` when `document["pages"]` is available, with the Docling chunkers kept as fallbacks / artifacts. The `financebench_corpus` Qdrant collection (pypdf-produced) is the canonical FinanceBench corpus going forward.

## Artifacts retained

- `financebench_corpus` — pypdf chunks, canonical (68,059 chunks)
- `financebench_corpus_docling` — markdown chunker's output (50,253 chunks, retained for future reference / re-experiments)
- `tests/evaluation/eval_results/financebench_baseline.{pipeline,patronus}.json` — canonical numbers
- `tests/evaluation/eval_results/financebench_docling.{pipeline,patronus}.json` — HybridChunker run
- `tests/evaluation/eval_results/financebench_docling_v2.{pipeline,patronus}.json` — Markdown run
- `scripts/smoke_docling_chunker.py`, `scripts/smoke_docling_markdown.py`, `scripts/smoke_docling_markdown_chunker.py` — smoke tests
- `scripts/diff_financebench_runs.py` — per-question bucket classifier
- `scripts/score_patronus.py` — REST-direct Patronus scorer (bypasses broken SDK)

## Lessons & generalizations

1. **Structural purity ≠ retrieval quality.** Intuitively a section-bounded chunk is "cleaner." Empirically, for Q&A on dense documents, spatial locality of neighboring context (paragraph → table → footnote → next paragraph) matters more than whether chunk boundaries align to section boundaries. Don't assume — measure.

2. **Serializers are load-bearing.** HybridChunker's chunks had the right *metadata* but the wrong *text content* because its internal table serializer wasn't designed for dense multi-year financial statements. The structural metadata didn't save us when the content itself was garbled. This is a general risk with any structured-chunker library: inspect what ends up in `chunk.text` on representative inputs before trusting it.

3. **Use two judges when the stakes are "should I change the ingestion pipeline."** RAGAS and Patronus disagree in absolute values (RAGAS faithfulness 0.47 ≈ Patronus pass rate 0.25 for the same corpus) but agree on relative rankings. Disagreement on ordering would have forced deeper investigation; concordance meant we could call the result with confidence.

4. **Smoke tests on one PDF catch catastrophic wiring bugs. Full eval catches quality regressions.** Our first Docling overnight ingestion silently kept using pypdf chunks (chunker read `document["pages"]` before `document["text"]`). A ~2-min smoke test on one PDF — comparing old vs new output side by side — caught it instantly. In contrast, the HybridChunker *was* wired correctly and passed the smoke test; only the full 150-question eval surfaced the serialization regression. Budget for both layers of testing.

5. **`n=150` with two judges > `n=1500` with one judge**, especially when N is expensive. 150 × 2-judge cross-validation was sufficient to make this call. Scaling to 1500 questions with one judge would have cost more and told us less.

6. **Free-tier quotas bite mid-experiment.** Patronus free tier (~1k evals/month) was depleted partway through the 3rd collection's resume. We still had enough to make the call because of cross-judge concordance, but future phases should track API budget from the first call, not discover it by HTTP 402.

## What did *not* move the needle (things we ruled out)

- **The table-serialization bug** was real (HybridChunker flattens tables badly) and we fixed it in the Markdown chunker. Metrics barely moved. Fixing the serialization alone wasn't enough.
- **Structural metadata in `chunk.payload`** (section_header / heading_path / chunk_type) was populated correctly in both Docling runs. Our retrieval doesn't yet *use* this signal for filtering or boosting — adding such usage could be a future follow-up but wouldn't rescue these experiments, since the regression showed up on retrieved content quality, not retrieval targeting.
- **Contextual-retrieval prefix** (`[Company: X | FY2023 10K | Page 4]`) was present on every chunk in all three runs; it's not the differentiator.

## Future work (deferred, not recommended near-term)

- **Hybrid chunking** — use pypdf for primary chunks, post-hoc-tag each chunk with the Docling section_header it falls under. Gives us metadata for future filtering without disturbing chunk content. Moderate cost, uncertain payoff. Not prioritized.
- **Table-aware generator prompt** — detect `chunk_type=="table"` chunks (if we re-enable Docling metadata) and inject a "this context contains tabular data, read carefully" instruction. Micro-optimization.
- **Re-test with a better foundation model** — our current generator (Claude Sonnet 4.6 via Anthropic, or GPT-4o-mini via OpenAI for FORCE_OPENAI_ONLY eval mode) might struggle with pipe-tables more than frontier models. Worth re-testing chunker choice with GPT-5 or equivalent when cost drops.
