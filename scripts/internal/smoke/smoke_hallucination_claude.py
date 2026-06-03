"""Smoke test: hallucination_checker_node with Claude Sonnet 4.6 + max_tokens=1024.

Loads 5 (question, answer, contexts) triples from the existing docling pipeline
cache, runs the actual hallucination_checker_node against each one, and asserts
that no ValidationError fallback path fires. If any sample triggers the
"Hallucination check failed, assuming grounded" error path, the test exits 1.

Costs ~$0.05 of Anthropic spend. Logged to cost_logs under run_id
'smoke_hallucination_claude'.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

CACHE_CANDIDATES = [
    Path("tests/evaluation/eval_results/financebench_docling_clean.pipeline.json"),
    Path("tests/evaluation/eval_results/after_sprint7b_claude_sonnet.pipeline.json"),
]

# Pick a mix: short answer, long answer, multi-chunk, single-chunk.
# Indices chosen empirically from the docling cache to exercise different sizes.
SAMPLE_INDICES = [0, 3, 7, 50, 120]


def _load_cache() -> dict:
    for p in CACHE_CANDIDATES:
        if p.exists():
            return json.loads(p.read_text())
    raise FileNotFoundError(f"None of {CACHE_CANDIDATES} exists.")


class _ErrorCaptureHandler(logging.Handler):
    """Captures ERROR-level records from src.graph.nodes.hallucination."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.endswith("hallucination") or "Hallucination check failed" in record.getMessage():
            self.records.append(record)


def main() -> int:
    cache = _load_cache()
    n = len(cache["questions"])
    indices = [i for i in SAMPLE_INDICES if i < n][:5]
    print(f"Loaded {n} samples from cache; testing indices {indices}")

    capture = _ErrorCaptureHandler()
    logging.getLogger().addHandler(capture)

    # Import after potential env tweaks
    from src.graph.nodes.hallucination import hallucination_checker_node
    from src.services.cost_tracker import CostTracker

    failures: list[tuple[int, str]] = []
    fallback_count = 0

    with CostTracker.run("smoke_hallucination_claude"):
        for i in indices:
            q = cache["questions"][i]
            a = cache["answers"][i]
            ctxs = cache["contexts"][i] or []
            chunks = [
                {"raw_content": c, "metadata": {"source_file": f"chunk_{j}"}}
                for j, c in enumerate(ctxs)
            ]
            state = {
                "question": q,
                "generated_answer": a,
                "relevant_chunks": chunks,
                "user_role": "admin",
                "generation_retry_count": 0,
            }
            print(f"\n[{i}] Q: {q[:80]}")
            print(f"     A: {a[:80]}{'...' if len(a) > 80 else ''}")
            print(f"     ctxs: {len(chunks)} chunk(s), total {sum(len(c) for c in ctxs):,} chars")

            try:
                result = hallucination_checker_node(state)
            except Exception as exc:  # noqa: BLE001
                failures.append((i, f"raised {type(exc).__name__}: {exc}"))
                print(f"     ❌ EXCEPTION: {exc}")
                continue

            status = result.get("hallucination_status")
            score = result.get("hallucination_score")
            print(f"     -> grounded={status} score={score:.3f}")

            # If the score is exactly 0.5 AND status is grounded, that's the
            # exception-fallback fingerprint.
            if status == "grounded" and score == 0.5:
                fallback_count += 1

    # Detect the ValidationError fallback path via captured ERROR logs
    captured_errors = [
        r for r in capture.records
        if "assuming grounded" in r.getMessage() or "ValidationError" in r.getMessage()
    ]

    print("\n=== Summary ===")
    print(f"  samples tested: {len(indices)}")
    print(f"  exception fallbacks (ERROR logs): {len(captured_errors)}")
    print(f"  score==0.5+grounded fingerprints: {fallback_count}")
    print(f"  hard failures: {len(failures)}")

    # Cost summary
    summary = CostTracker.summarize(run_id="smoke_hallucination_claude")
    run_data = summary["runs"].get("smoke_hallucination_claude", {})
    if run_data:
        print(f"  cost: ${run_data['cost_usd']:.4f} across {run_data['calls']} calls")
        for model, stats in run_data["models"].items():
            print(f"    {model:<32} ${stats['cost_usd']:.4f}  in={int(stats['input_tokens']):,}  out={int(stats['output_tokens']):,}")

    if captured_errors or failures:
        print("\n❌ FAIL — at least one hallucination check fell through to the exception path")
        for r in captured_errors:
            print(f"   log: {r.getMessage()[:200]}")
        for idx, msg in failures:
            print(f"   sample {idx}: {msg}")
        return 1

    print("\n✅ PASS — every Claude hallucination check produced a valid HallucinationCheck")
    return 0


if __name__ == "__main__":
    sys.exit(main())
