"""Doc2Query BM25 enrichment — generate synthetic predicted-questions per chunk
and append them to the BM25 sparse vector.

Updates the sparse vector slot in-place via Qdrant `update_vectors`. The dense
vector is left untouched. Falls back gracefully if doc2query produces empty
output for a chunk (just keeps the original sparse vector).

Usage (targeted by company — Day 8 experiment):
    python scripts/enrich_bm25_doc2query.py \\
      --collection financebench_corpus_pypdf_emb_large \\
      --filter-company ulta_beauty \\
      --questions-per-chunk 3 \\
      --batch-size 8

Usage (full collection):
    python scripts/enrich_bm25_doc2query.py \\
      --collection financebench_corpus_pypdf_emb_large \\
      --questions-per-chunk 3 \\
      --batch-size 8

Optimizations vs the smoke baseline:
  - fp16 weights on MPS (~2× speedup, ~4× lower memory)
  - Batched generation (batch_size=8 → ~5-8× wall-clock vs unbatched)
  - Reduced num_return_sequences (3 vs the smoke's 5)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch  # noqa: E402
from qdrant_client.models import (  # noqa: E402
    FieldCondition,
    Filter,
    MatchValue,
    NamedSparseVector,
    PointVectors,
    SparseVector,
)
from tqdm import tqdm  # noqa: E402
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer  # noqa: E402

from src.services.vector_store import (  # noqa: E402
    SPARSE_VECTOR_NAME,
    compute_sparse_vectors,
    get_qdrant_client,
)


def _load_model(device: str, dtype: torch.dtype):
    name = "doc2query/msmarco-t5-small-v1"
    print(f"Loading {name} on {device} (dtype={dtype})...")
    tok = AutoTokenizer.from_pretrained(name)
    model = AutoModelForSeq2SeqLM.from_pretrained(name)
    if dtype != torch.float32:
        model = model.to(dtype=dtype)
    model = model.to(device)
    model.eval()
    return tok, model


def _generate_questions(
    tok,
    model,
    device: str,
    contents: list[str],
    n_questions: int,
    max_input_length: int = 512,
    max_output_length: int = 64,
) -> list[list[str]]:
    """For each chunk in `contents`, return a list of `n_questions` synthetic queries."""
    inputs = tok(
        contents,
        return_tensors="pt",
        truncation=True,
        max_length=max_input_length,
        padding=True,
    ).to(device)
    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_length=max_output_length,
            do_sample=True,
            top_k=10,
            num_return_sequences=n_questions,
        )
    decoded = [tok.decode(o, skip_special_tokens=True) for o in outputs]
    # outputs is shape (batch * n_questions); regroup
    return [
        decoded[i * n_questions : (i + 1) * n_questions] for i in range(len(contents))
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Enrich Qdrant BM25 sparse vectors with doc2query synthetic questions")
    parser.add_argument("--collection", required=True, help="Source collection (sparse vectors will be updated in-place)")
    parser.add_argument("--filter-company", default=None, help="Optional: only enrich chunks where payload.company == this slug")
    parser.add_argument("--questions-per-chunk", type=int, default=3, help="Synthetic questions per chunk (default: 3)")
    parser.add_argument("--batch-size", type=int, default=8, help="Generation batch size (default: 8)")
    parser.add_argument("--scroll-size", type=int, default=128, help="Qdrant scroll page size (default: 128)")
    parser.add_argument("--limit", type=int, default=None, help="Stop after N chunks (smoke / partial enrichment)")
    parser.add_argument("--no-fp16", action="store_true", help="Disable fp16 (use fp32 for debugging)")
    args = parser.parse_args()

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    dtype = torch.float32 if args.no_fp16 else torch.float16
    tok, model = _load_model(device, dtype)

    client = get_qdrant_client()
    qdrant_filter = (
        Filter(must=[FieldCondition(key="company", match=MatchValue(value=args.filter_company))])
        if args.filter_company
        else None
    )

    # Count + scroll
    n_total = client.count(collection_name=args.collection, count_filter=qdrant_filter).count
    if args.limit:
        n_total = min(n_total, args.limit)
    filter_msg = f"company={args.filter_company}" if args.filter_company else "ALL chunks"
    print(f"Source: {args.collection} ({filter_msg}) — {n_total:,} chunks to enrich\n")

    pbar = tqdm(total=n_total, desc="enrich", unit="chunk")
    n_done = 0
    n_skipped = 0
    start = time.time()

    offset = None
    batch_buffer: list = []  # list of (point_id, content)
    while n_done < n_total:
        points, offset = client.scroll(
            collection_name=args.collection,
            scroll_filter=qdrant_filter,
            limit=args.scroll_size,
            with_payload=True,
            with_vectors=False,
            offset=offset,
        )
        if not points:
            break

        for p in points:
            content = (p.payload or {}).get("content", "")
            if not content:
                n_skipped += 1
                continue
            # Truncate to a sensible max — doc2query trained on ~256 token passages
            batch_buffer.append((p.id, content[:1500]))

            if len(batch_buffer) >= args.batch_size:
                _flush_batch(
                    client=client,
                    collection=args.collection,
                    tok=tok,
                    model=model,
                    device=device,
                    batch=batch_buffer,
                    n_questions=args.questions_per_chunk,
                )
                pbar.update(len(batch_buffer))
                n_done += len(batch_buffer)
                batch_buffer = []
                if args.limit and n_done >= args.limit:
                    break
        if offset is None:
            break
        if args.limit and n_done >= args.limit:
            break

    # Flush trailing batch
    if batch_buffer and (not args.limit or n_done < args.limit):
        _flush_batch(
            client=client,
            collection=args.collection,
            tok=tok,
            model=model,
            device=device,
            batch=batch_buffer,
            n_questions=args.questions_per_chunk,
        )
        pbar.update(len(batch_buffer))
        n_done += len(batch_buffer)

    pbar.close()
    elapsed = time.time() - start
    rate = n_done / elapsed if elapsed > 0 else 0
    print()
    print(f"Done in {elapsed:.1f}s ({elapsed / 60:.1f} min)")
    print(f"  enriched: {n_done:,}")
    print(f"  skipped (empty content): {n_skipped:,}")
    print(f"  rate: {rate:.1f} chunks/sec")
    if rate > 0:
        print(f"  projection for 68k chunks: {68_059 / rate / 60:.1f} min")
    return 0


def _flush_batch(client, collection, tok, model, device, batch, n_questions):
    """Generate predictions for a batch and update sparse vectors in place."""
    point_ids = [pid for pid, _ in batch]
    contents = [content for _, content in batch]

    questions_per_chunk = _generate_questions(
        tok=tok, model=model, device=device,
        contents=contents, n_questions=n_questions,
    )

    # For each chunk: enriched_text = content + " " + " ".join(questions)
    enriched_texts = [
        content + " " + " ".join(qs)
        for content, qs in zip(contents, questions_per_chunk)
    ]
    new_sparse = compute_sparse_vectors(enriched_texts)

    # Update sparse vector in place; dense untouched
    point_vectors = [
        PointVectors(
            id=pid,
            vector={
                SPARSE_VECTOR_NAME: SparseVector(
                    indices=list(s.indices), values=list(s.values)
                )
            },
        )
        for pid, s in zip(point_ids, new_sparse)
    ]
    client.update_vectors(collection_name=collection, points=point_vectors)


if __name__ == "__main__":
    sys.exit(main())
