"""FinanceBench Phase A — one-question proof of life.

Minimum viable integration:
  1. Load a single FinanceBench Q&A record (default: financebench_id_00552,
     an FY2023 Microsoft balance-sheet question that our existing corpus can answer).
  2. Route it through the full 16-node graph exactly as our standard eval does.
  3. Score the result with RAGAS (Faithfulness, AnswerRelevancy, AnswerCorrectness).
  4. Print per-metric numbers + the pass/fail vs our CI thresholds.

Nothing about the graph changes. This only exercises the adapter path —
can we map a FinanceBench record into our RAGState, run the graph, and score
the output with RAGAS metrics against the FB ground truth?

Usage:
    python tests/evaluation/run_financebench_phase_a.py
    python tests/evaluation/run_financebench_phase_a.py --id financebench_id_00552
    FORCE_OPENAI_ONLY=true python tests/evaluation/run_financebench_phase_a.py  # cheap
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage

from src.config.settings import settings
from src.graph.builder import build_graph

# Mirror OPENAI_API_KEY for RAGAS evaluator (same pattern as run_evaluation.py)
if settings.OPENAI_API_KEY and not os.environ.get("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = settings.OPENAI_API_KEY

# Silence noisy loggers
for _n in [
    "httpx", "httpcore", "presidio-analyzer", "presidio-anonymizer",
    "openai", "langchain", "langchain_core", "langgraph",
    "qdrant_client", "py.warnings", "urllib3", "llm_guard",
]:
    logging.getLogger(_n).setLevel(logging.WARNING)
for _m in ["src.graph.nodes", "src.services"]:
    logging.getLogger(_m).setLevel(logging.WARNING)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

FB_DATA_DIR = Path("data/raw/financebench")
FB_QA_PATH = FB_DATA_DIR / "financebench_open_source.jsonl"
DEFAULT_ID = "financebench_id_00552"  # FY2023 MSFT debt question


def _build_initial_state(question: str) -> dict:
    """Match run_evaluation.py's state init. Admin role + hitl_enabled=False."""
    return {
        "messages": [HumanMessage(content=question)],
        "user_id": "financebench_phase_a",
        "user_role": "admin",
        "allowed_doc_types": [],
        "guardrail_status": "clean",
        "detected_pii_entities": [],
        "sanitized_query": "",
        "query_intent": "",
        "target_company": None,
        "target_fiscal_year": None,
        "retrieved_chunks": [],
        "reranked_chunks": [],
        "retrieval_query": "",
        "relevant_chunks": [],
        "grading_results": [],
        "generated_answer": "",
        "hallucination_status": "",
        "hallucination_score": 0.0,
        "requires_human_approval": False,
        "human_decision": None,
        "retrieval_retry_count": 0,
        "generation_retry_count": 0,
        "final_response": "",
        "response_metadata": {},
    }


def fb_record(rid: str) -> dict:
    if not FB_QA_PATH.exists():
        print(f"ERROR: {FB_QA_PATH} not found. Run scripts/download_financebench.py first.")
        sys.exit(1)
    for line in open(FB_QA_PATH):
        rec = json.loads(line)
        if rec["financebench_id"] == rid:
            return rec
    print(f"ERROR: no record with id={rid}")
    sys.exit(1)


def run_pipeline(record: dict) -> dict:
    question = record["question"]
    logger.info(f"Pipeline on: {question}")

    graph = build_graph(checkpointer=None)
    state = _build_initial_state(question)
    config = {
        "configurable": {"thread_id": f"fb_{record['financebench_id']}"},
        "metadata": {"hitl_enabled": False},
    }
    result = graph.invoke(state, config=config)
    answer = result.get("final_response", "")
    chunks = [c["content"] for c in result.get("relevant_chunks", []) if "content" in c]
    return {"answer": answer, "contexts": chunks if chunks else [""]}


def score_with_ragas(record: dict, pipeline_out: dict) -> dict:
    from ragas import EvaluationDataset, SingleTurnSample, evaluate
    from ragas.llms import llm_factory
    from ragas.metrics import (
        AnswerRelevancy,
        ContextPrecision,
        ContextRecall,
        Faithfulness,
    )

    evaluator_llm = llm_factory("gpt-4o-mini")
    metrics = [
        Faithfulness(llm=evaluator_llm),
        AnswerRelevancy(llm=evaluator_llm),
        ContextPrecision(llm=evaluator_llm),
        ContextRecall(llm=evaluator_llm),
    ]

    sample = SingleTurnSample(
        user_input=record["question"],
        response=pipeline_out["answer"],
        retrieved_contexts=pipeline_out["contexts"],
        reference=record["answer"],
    )
    ds = EvaluationDataset(samples=[sample])
    results = evaluate(dataset=ds, metrics=metrics, show_progress=False)
    df = results.to_pandas()
    return {
        "faithfulness": float(df["faithfulness"].iloc[0]),
        "answer_relevancy": float(df["answer_relevancy"].iloc[0]),
        "context_precision": float(df["context_precision"].iloc[0]),
        "context_recall": float(df["context_recall"].iloc[0]),
    }


def main():
    parser = argparse.ArgumentParser(description="FinanceBench Phase A — one-question proof of life")
    parser.add_argument("--id", default=DEFAULT_ID, help="FinanceBench record id")
    args = parser.parse_args()

    record = fb_record(args.id)
    print("=" * 80)
    print(f"Record: {record['financebench_id']}")
    print(f"Company: {record['company']} | Doc: {record['doc_name']}")
    print(f"Question: {record['question']}")
    print(f"Ground truth: {record['answer']}")
    print(f"Evidence pages: {[e.get('evidence_page_num') for e in record.get('evidence', [])]}")
    print("=" * 80)
    print()

    pipeline_out = run_pipeline(record)
    print(f"Pipeline answer ({len(pipeline_out['answer'])} chars):")
    print(f"  {pipeline_out['answer']}")
    print(f"Relevant chunks used: {len(pipeline_out['contexts'])}")
    print()

    print("Scoring with RAGAS...")
    scores = score_with_ragas(record, pipeline_out)
    print()
    print("=" * 80)
    print("RAGAS scores on this single record:")
    for k, v in scores.items():
        print(f"  {k:20s} {v:.3f}")
    print("=" * 80)


if __name__ == "__main__":
    main()
