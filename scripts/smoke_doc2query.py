"""Smoke test for doc2query/msmarco-t5-small-v1 on financial chunks.

Pulls 5 random chunks from the FB Qdrant collection, generates 3-5
synthetic predicted-questions per chunk, prints them so we can eyeball
quality before committing to the full 68k-chunk enrichment run.

Validates:
  - Model loads on MPS without errors
  - Generation produces coherent English questions
  - Predictions are plausibly relevant to the source chunk's content
    (financial 10-K language, even though doc2query was trained on MSMARCO)
  - Per-chunk inference time is reasonable for the full 68k-chunk run
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import torch  # noqa: E402
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer  # noqa: E402

from src.services.vector_store import get_qdrant_client  # noqa: E402

MODEL_NAME = "doc2query/msmarco-t5-small-v1"
COLLECTION = "financebench_corpus_pypdf_emb_large"
N_QUESTIONS = 5
N_CHUNKS = 5


def main() -> int:
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"Loading {MODEL_NAME} on {device}...")
    tok = AutoTokenizer.from_pretrained(MODEL_NAME)
    model = AutoModelForSeq2SeqLM.from_pretrained(MODEL_NAME).to(device)
    model.eval()
    print(f"  {sum(p.numel() for p in model.parameters()):,} params loaded\n")

    # Pull 5 sample chunks
    client = get_qdrant_client()
    points, _ = client.scroll(
        collection_name=COLLECTION,
        limit=N_CHUNKS,
        with_payload=True,
        with_vectors=False,
    )

    print(f"=== Smoke: {len(points)} chunks ===\n")
    total_t = 0.0
    for i, p in enumerate(points, 1):
        content = (p.payload or {}).get("content", "")
        company = (p.payload or {}).get("company", "?")
        page = (p.payload or {}).get("page_number", "?")
        # Truncate input — doc2query was trained on ~256-token passages
        chunk_input = content[:1500]

        print(f"[{i}/{len(points)}] {company} | p{page} | {len(content)} chars")
        print(f"  Source (300c): {chunk_input[:300]!r}")

        t0 = time.time()
        ids = tok([chunk_input], return_tensors="pt", truncation=True, max_length=512).to(device)
        with torch.no_grad():
            out = model.generate(
                **ids,
                max_length=64,
                do_sample=True,
                top_k=10,
                num_return_sequences=N_QUESTIONS,
            )
        questions = [tok.decode(o, skip_special_tokens=True) for o in out]
        elapsed = time.time() - t0
        total_t += elapsed

        print(f"  → generated in {elapsed:.2f}s:")
        for q in questions:
            print(f"      • {q}")
        print()

    avg = total_t / len(points)
    print(f"Average per-chunk: {avg * 1000:.0f}ms")
    print(f"Projected for 68k chunks: {68_059 * avg / 60:.1f} min ({68_059 * avg / 3600:.1f} hours)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
