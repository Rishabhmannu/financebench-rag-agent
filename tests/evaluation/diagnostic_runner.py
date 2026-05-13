"""Sprint 7.15 Phase 0 — per-node diagnostic on the 75-Q labeled set.

For each labeled record, invoke each pipeline node in isolation and capture
its output. Compare against human labels. Compute F1 / accuracy per component.

Nodes measured:
  - Router (intent + complexity)
  - Entity extractor (company + year)
  - Hallucination checker (grounded vs hallucinated on V1 answer + sources)
  - Research-agent decomposer (sub-queries for research_required Qs only)

Plus decomposition-coverage scoring via Sonnet-as-judge.

Output: tests/evaluation/eval_results/pipeline_diagnostic_results.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from src.graph.nodes.entity_extractor import entity_extractor_node
from src.graph.nodes.hallucination import hallucination_checker_node
from src.graph.nodes.router import router_node
from src.graph.nodes.research_agent import _decompose

DIAG_JSONL = Path("tests/evaluation/pipeline_diagnostic_v1.jsonl")
LABELS_JSON = Path("tests/evaluation/pipeline_diagnostic_manual_labels_summary.json")
OUTPUT = Path("tests/evaluation/eval_results/pipeline_diagnostic_results.json")
PARALLELISM = 4


# ---- Decomposition coverage judge ----

class DecompCoverage(BaseModel):
    coverage_score: float = Field(
        ge=0.0, le=1.0,
        description="0-1 score on how well the system's sub-queries cover the human's expected ones."
    )
    classification: str = Field(
        description="One of: equivalent, partial_match, missed_items, wrong_focus"
    )
    reason: str = Field(description="One sentence explaining the score.")


DECOMP_PROMPT = """Compare a human's expected decomposition of a financial question against the system's actual decomposition. Score how well the system's sub-queries cover the same intent and information requirements.

Original question:
{question}

Human-expected sub-queries (ground truth):
{human_subq}

System's actual sub-queries:
{system_subq}

Score 0.0–1.0 where:
- 1.0: equivalent — system covers all the same information needs
- 0.7–0.9: partial_match — covers most but missed some sub-aspect
- 0.4–0.6: missed_items — significant items missing or substituted
- 0.0–0.3: wrong_focus — system fundamentally misunderstood the decomposition

Also classify: equivalent / partial_match / missed_items / wrong_focus
"""


def _make_decomp_judge():
    llm = ChatAnthropic(
        model="claude-sonnet-4-6",
        temperature=0.0,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        max_tokens=512,
    )
    return llm.with_structured_output(DecompCoverage)


def _judge_decomposition(judge, question, human_subq, system_subq):
    if isinstance(human_subq, list):
        human_str = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(human_subq))
    else:
        human_str = str(human_subq)
    system_str = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(system_subq))
    prompt = DECOMP_PROMPT.format(question=question, human_subq=human_str, system_subq=system_str)
    try:
        v: DecompCoverage = judge.invoke([HumanMessage(content=prompt)])
        return v.coverage_score, v.classification, v.reason
    except Exception as e:
        return 0.0, "error", f"judge_error: {type(e).__name__}: {e}"


# ---- Node invocations ----

def _run_router(question: str) -> dict:
    state = {"sanitized_query": question}
    try:
        return router_node(state)
    except Exception as e:
        return {"query_intent": None, "query_complexity": None, "_error": str(e)[:200]}


def _run_entity_extractor(question: str) -> dict:
    state = {"sanitized_query": question, "messages": [HumanMessage(content=question)]}
    try:
        return entity_extractor_node(state)
    except Exception as e:
        return {"target_company": None, "target_fiscal_year": None, "_error": str(e)[:200]}


def _run_hallu(answer: str, chunks_str: list[str]) -> dict:
    """Wrap V1's chunk strings into chunk dicts for the hallu node."""
    chunks = [
        {"content": s, "raw_content": s, "metadata": {"source_file": "v1_retrieval"}}
        for s in chunks_str if s
    ]
    state = {
        "generated_answer": answer,
        "relevant_chunks": chunks,
        "user_role": "analyst",
        "generation_retry_count": 0,
    }
    try:
        return hallucination_checker_node(state)
    except Exception as e:
        return {"hallucination_status": None, "_error": str(e)[:200]}


def _run_decompose(question: str, target_company: str | None, target_year: int | None) -> list[str]:
    try:
        result = _decompose(question, target_company, target_year)
        return list(result.sub_questions or [])
    except Exception as e:
        return [f"decompose_error: {type(e).__name__}: {e}"]


# ---- F1 helpers ----

def _compute_f1(items, get_pred, get_truth, labels):
    """Multi-class F1. Returns (overall_acc, per_class_f1, macro_f1, confusion_matrix)."""
    from collections import Counter
    n = len(items)
    if n == 0:
        return 0.0, {}, 0.0, {}
    correct = 0
    confusion: dict[tuple, int] = Counter()
    for item in items:
        truth = get_truth(item)
        pred = get_pred(item)
        confusion[(truth, pred)] += 1
        if truth == pred:
            correct += 1
    acc = correct / n
    per_class_f1 = {}
    for label in labels:
        tp = confusion.get((label, label), 0)
        fp = sum(confusion.get((t, label), 0) for t in labels if t != label)
        fn = sum(confusion.get((label, p), 0) for p in labels if p != label)
        p = tp / (tp + fp) if (tp + fp) else 0.0
        r = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * p * r / (p + r) if (p + r) else 0.0
        per_class_f1[label] = {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f1, 4), "tp": tp, "fp": fp, "fn": fn}
    macro_f1 = sum(c["f1"] for c in per_class_f1.values()) / len(labels) if labels else 0.0
    return round(acc, 4), per_class_f1, round(macro_f1, 4), {f"{t}__{p}": n for (t, p), n in confusion.items()}


# ---- Main ----

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    records = [json.loads(line) for line in open(DIAG_JSONL) if line.strip()]
    labels = json.load(open(LABELS_JSON))["records"]
    labels_by_id = {l["record"]: l for l in labels}

    if args.limit:
        records = records[: args.limit]
    print(f"loaded {len(records)} records")

    decomp_judge = _make_decomp_judge()

    # ---- Parallel node invocations ----

    def measure_one(rec):
        question = rec["question"]
        rid = rec["id"]
        out = {"id": rid, "fb_id": rec["fb_id"], "question": question[:200]}

        # Router
        router_out = _run_router(question)
        out["router_intent"] = router_out.get("query_intent")
        out["router_complexity"] = router_out.get("query_complexity")
        if router_out.get("_error"):
            out["router_error"] = router_out["_error"]

        # Entity extractor
        ent_out = _run_entity_extractor(question)
        out["entity_company"] = ent_out.get("target_company")
        out["entity_year"] = ent_out.get("target_fiscal_year")
        if ent_out.get("_error"):
            out["entity_error"] = ent_out["_error"]

        # Hallu — feed V1's answer + V1's retrieved chunks
        hallu_out = _run_hallu(rec["v1_system_answer"], rec["v1_retrieved_chunks_top"])
        out["hallu_status"] = hallu_out.get("hallucination_status")
        out["hallu_score"] = hallu_out.get("hallucination_score")
        if hallu_out.get("_error"):
            out["hallu_error"] = hallu_out["_error"]

        return out

    t0 = time.time()
    print(f"\nrunning router/entity/hallu on {len(records)} records in parallel ({PARALLELISM} workers)...")
    measurements = [None] * len(records)
    with ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
        futures = {pool.submit(measure_one, rec): i for i, rec in enumerate(records)}
        done = 0
        for fut in as_completed(futures):
            i = futures[fut]
            measurements[i] = fut.result()
            done += 1
            if done % 10 == 0:
                print(f"  [{done}/{len(records)}] done ({time.time() - t0:.0f}s)")

    print(f"\nrouter+entity+hallu pass: {time.time() - t0:.0f}s")

    # ---- Decomposer on research_required Qs ----

    print(f"\nrunning decomposer on research_required cases...")
    research_records = [
        (i, rec) for i, rec in enumerate(records)
        if labels_by_id.get(rec["id"], {}).get("complexity") == "research_required"
    ]
    print(f"  {len(research_records)} research_required records to decompose")

    def measure_decomp(args_tuple):
        i, rec = args_tuple
        out = {}
        ent_out = measurements[i]
        target_company = ent_out.get("entity_company")
        target_year = ent_out.get("entity_year")
        subq = _run_decompose(rec["question"], target_company, target_year)
        out["system_sub_queries"] = subq
        return i, out

    t1 = time.time()
    with ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
        for fut in as_completed([pool.submit(measure_decomp, x) for x in research_records]):
            i, out = fut.result()
            measurements[i].update(out)
    print(f"decompose pass: {time.time() - t1:.0f}s")

    # ---- Judge decomposition coverage ----

    print(f"\njudging decomposition coverage with Sonnet...")
    t2 = time.time()
    for i, rec in research_records:
        m = measurements[i]
        l = labels_by_id.get(rec["id"], {})
        # Parse human's sub-queries from the markdown — need to do that separately
        # For now, store placeholders; we'll fill from the labeled markdown
        m["decomp_coverage_pending"] = True

    # Parse human sub-queries from the markdown
    print("  parsing human sub-queries from labeled markdown...")
    import re
    md = Path("tests/evaluation/pipeline_diagnostic_v1_labeled.md").read_text()
    md_blocks = re.split(r"^## Record \d+ of \d+\s+—\s+`([^`]+)`", md, flags=re.MULTILINE)
    human_subq_by_id = {}
    for k in range(1, len(md_blocks) - 1, 2):
        rid = md_blocks[k].strip()
        body = md_blocks[k + 1]
        subq_match = re.search(r"\*\*EXPECTED_SUB_QUERIES:\*\*\s*\n(.+?)(?=\n\*\*[A-Z_]+:\*\*|\n---)", body, re.DOTALL)
        if subq_match:
            section = subq_match.group(1).strip()
            items = re.findall(r"^\s*\d+\.\s+(.+)$", section, re.MULTILINE)
            human_subq_by_id[rid] = items if items else section

    # Now judge each research_required Q
    for i, rec in research_records:
        m = measurements[i]
        rid = rec["id"]
        human_subq = human_subq_by_id.get(rid, [])
        system_subq = m.get("system_sub_queries", [])
        if not human_subq or not system_subq:
            m["decomp_score"] = None
            m["decomp_class"] = "no_data"
            m["decomp_reason"] = f"missing data — human={len(human_subq) if isinstance(human_subq, list) else 'str'} system={len(system_subq)}"
            continue
        score, cls, reason = _judge_decomposition(decomp_judge, rec["question"], human_subq, system_subq)
        m["decomp_score"] = score
        m["decomp_class"] = cls
        m["decomp_reason"] = reason
        m["human_sub_queries"] = human_subq

    print(f"decomp coverage judging: {time.time() - t2:.0f}s")

    # ---- Compute metrics ----

    print(f"\ncomputing F1s vs human labels...")
    # Build joined records (with both label + measurement)
    joined = []
    for rec, m in zip(records, measurements):
        l = labels_by_id.get(rec["id"], {})
        if not l:
            continue
        joined.append({
            **m,
            "human_intent": l.get("intent"),
            "human_complexity": l.get("complexity"),
            "human_target_company_ok": l.get("target_company") == "OK",
            "human_target_year_ok": l.get("target_year") == "OK",
            "human_hallu_grounded": l.get("hallu_grounded"),
            "auto_target_company": rec["auto_target_company_slug"],
            "auto_target_year": rec["auto_target_fiscal_year"],
            "v1_pass_status": rec["v1_pass_status"],
            "v1_audit_category": rec["v1_audit_category"],
        })

    # Router intent F1
    intent_acc, intent_f1, intent_macro, intent_conf = _compute_f1(
        joined,
        get_pred=lambda x: x["router_intent"],
        get_truth=lambda x: x["human_intent"],
        labels=["retrieval", "clarification", "out_of_scope"],
    )

    # Router complexity F1 (only on retrieval intent records — others have None)
    complexity_records = [j for j in joined if j["human_intent"] == "retrieval"]
    complexity_acc, complexity_f1, complexity_macro, complexity_conf = _compute_f1(
        complexity_records,
        get_pred=lambda x: x["router_complexity"],
        get_truth=lambda x: x["human_complexity"],
        labels=["simple_lookup", "research_required"],
    )

    # Entity company accuracy: extractor's company == auto_target_company (human says OK)
    entity_company_correct = sum(
        1 for j in joined
        if j["human_target_company_ok"] and j["entity_company"] == j["auto_target_company"]
    )
    entity_company_total = sum(1 for j in joined if j["human_target_company_ok"])
    entity_company_acc = entity_company_correct / entity_company_total if entity_company_total else 0.0

    # Entity year accuracy
    entity_year_correct = sum(
        1 for j in joined
        if j["human_target_year_ok"] and j["entity_year"] == j["auto_target_year"]
    )
    entity_year_total = sum(1 for j in joined if j["human_target_year_ok"])
    entity_year_acc = entity_year_correct / entity_year_total if entity_year_total else 0.0

    # Hallucination checker: map human Y → grounded, N → hallucinated, PARTIAL → hallucinated (strict)
    def _hallu_truth_strict(j):
        h = j["human_hallu_grounded"]
        return "grounded" if h == "Y" else "hallucinated"
    def _hallu_truth_lenient(j):
        h = j["human_hallu_grounded"]
        return "hallucinated" if h == "N" else "grounded"

    hallu_strict_acc, hallu_strict_f1, hallu_strict_macro, _ = _compute_f1(
        joined,
        get_pred=lambda x: x["hallu_status"],
        get_truth=_hallu_truth_strict,
        labels=["grounded", "hallucinated"],
    )
    hallu_lenient_acc, hallu_lenient_f1, hallu_lenient_macro, _ = _compute_f1(
        joined,
        get_pred=lambda x: x["hallu_status"],
        get_truth=_hallu_truth_lenient,
        labels=["grounded", "hallucinated"],
    )

    # Decomposition coverage
    decomp_scores = [j["decomp_score"] for j in joined if j.get("decomp_score") is not None]
    decomp_mean = sum(decomp_scores) / len(decomp_scores) if decomp_scores else 0.0
    from collections import Counter
    decomp_classes = Counter(j["decomp_class"] for j in joined if j.get("decomp_class"))

    # ---- Output ----

    summary = {
        "n_records": len(joined),
        "router_intent": {
            "accuracy": intent_acc,
            "macro_f1": intent_macro,
            "per_class": intent_f1,
            "confusion": intent_conf,
        },
        "router_complexity": {
            "accuracy": complexity_acc,
            "macro_f1": complexity_macro,
            "per_class": complexity_f1,
            "confusion": complexity_conf,
        },
        "entity_extractor": {
            "company_accuracy": round(entity_company_acc, 4),
            "company_correct": entity_company_correct,
            "company_total": entity_company_total,
            "year_accuracy": round(entity_year_acc, 4),
            "year_correct": entity_year_correct,
            "year_total": entity_year_total,
        },
        "hallucination_checker": {
            "strict_mapping_partial_as_hallu": {
                "accuracy": hallu_strict_acc,
                "macro_f1": hallu_strict_macro,
                "per_class": hallu_strict_f1,
            },
            "lenient_mapping_partial_as_grounded": {
                "accuracy": hallu_lenient_acc,
                "macro_f1": hallu_lenient_macro,
                "per_class": hallu_lenient_f1,
            },
        },
        "decomposer": {
            "n_judged": len(decomp_scores),
            "mean_coverage_score": round(decomp_mean, 4),
            "class_distribution": dict(decomp_classes),
        },
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    out_data = {"summary": summary, "per_record": joined}
    OUTPUT.write_text(json.dumps(out_data, indent=2))
    print(f"\nwrote {OUTPUT}")

    print(f"\n=== SUMMARY ===")
    print(f"\nRouter intent:        acc={intent_acc:.3f}  macro-F1={intent_macro:.3f}")
    print(f"  per-class: {json.dumps({k: v['f1'] for k, v in intent_f1.items()})}")
    print(f"\nRouter complexity:    acc={complexity_acc:.3f}  macro-F1={complexity_macro:.3f}")
    print(f"  per-class: {json.dumps({k: v['f1'] for k, v in complexity_f1.items()})}")
    print(f"  confusion (truth__pred): {complexity_conf}")
    print(f"\nEntity company:       acc={entity_company_acc:.3f} ({entity_company_correct}/{entity_company_total})")
    print(f"Entity year:          acc={entity_year_acc:.3f} ({entity_year_correct}/{entity_year_total})")
    print(f"\nHallu (strict, PARTIAL=hallu): acc={hallu_strict_acc:.3f}  macro-F1={hallu_strict_macro:.3f}")
    print(f"  per-class: {json.dumps({k: v['f1'] for k, v in hallu_strict_f1.items()})}")
    print(f"Hallu (lenient, PARTIAL=grounded): acc={hallu_lenient_acc:.3f}  macro-F1={hallu_lenient_macro:.3f}")
    print(f"\nDecomposer coverage:  mean={decomp_mean:.3f}  classes={dict(decomp_classes)}")
    print(f"\nWall time total: {time.time() - t0:.0f}s")


if __name__ == "__main__":
    sys.exit(main())
