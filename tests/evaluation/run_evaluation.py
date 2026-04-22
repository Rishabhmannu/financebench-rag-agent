"""RAGAS evaluation runner for the RAG Agent pipeline.

Loads the evaluation dataset, runs each question through the full graph,
then scores the results using RAGAS metrics (faithfulness, answer relevancy,
context precision, context recall).

Usage:
    python tests/evaluation/run_evaluation.py --output eval_results.json
    python tests/evaluation/run_evaluation.py --check-thresholds
"""

import argparse
import json
import logging
import os
import sys
import time
import warnings
from pathlib import Path

from langchain_core.messages import HumanMessage
from tqdm import tqdm

from src.config.settings import settings
from src.graph.builder import build_graph
from tests.evaluation.eval_config import (
    EVAL_USER_ID,
    EVAL_USER_ROLE,
    EVALUATOR_MODEL,
    INFORMATIONAL_THRESHOLDS,
    THRESHOLDS,
)

# RAGAS internals instantiate their own ChatOpenAI from os.environ["OPENAI_API_KEY"],
# not from our Settings. Mirror the key into the OS env so RAGAS can find it.
if settings.OPENAI_API_KEY and not os.environ.get("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = settings.OPENAI_API_KEY

# --- Silence noisy third-party loggers so the progress bar stays readable ---
# Each HTTP call, every Presidio recognizer, and Pydantic serializer quirks would
# otherwise drown the eval progress. Keep WARNING+ so real failures still surface.
_NOISY_LOGGERS = [
    "httpx",
    "httpcore",
    "presidio-analyzer",
    "presidio-anonymizer",
    "openai",
    "langchain",
    "langchain_core",
    "langgraph",
    "qdrant_client",
    "py.warnings",
    "urllib3",
    "llm_guard",
]
for _name in _NOISY_LOGGERS:
    logging.getLogger(_name).setLevel(logging.WARNING)

# Silence the handful of UserWarnings from Pydantic/Qdrant version-skew that spam every call
warnings.filterwarnings("ignore", category=UserWarning, module="pydantic")
warnings.filterwarnings("ignore", category=UserWarning, module="qdrant_client")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Silence our own chatty per-node INFO logs during the pipeline loop (router, retrieval, grader, etc.)
# The tqdm bar + per-question post-summary give enough visibility.
for _module in ["src.graph.nodes", "src.services"]:
    logging.getLogger(_module).setLevel(logging.WARNING)

DATASET_PATH = Path(__file__).parent / "eval_dataset.json"


def load_dataset(path: Path) -> list[dict]:
    """Load evaluation dataset from JSON file."""
    with open(path) as f:
        data = json.load(f)
    logger.info(f"Loaded {len(data)} evaluation samples from {path}")
    return data


def build_initial_state(question: str) -> dict:
    """Build RAGState for a single evaluation question."""
    return {
        "messages": [HumanMessage(content=question)],
        "user_id": EVAL_USER_ID,
        "user_role": EVAL_USER_ROLE,
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


def run_rag_pipeline(graph, samples: list[dict]) -> tuple[list[str], list[list[str]]]:
    """Run each question through the RAG graph and collect answers + contexts.

    Returns (answers, contexts) where contexts is a list of lists of strings.
    """
    answers = []
    contexts = []
    failures = 0

    pbar = tqdm(samples, desc="Running pipeline", unit="q", ncols=100)
    for i, sample in enumerate(pbar):
        question = sample["question"]
        pbar.set_postfix_str(f"Q: {question[:50]}")

        state = build_initial_state(question)
        config = {
            "configurable": {"thread_id": f"eval_{i}"},
            "metadata": {"hitl_enabled": False},
        }

        try:
            result = graph.invoke(state, config=config)
            answer = result.get("final_response", "")
            chunks = [c["content"] for c in result.get("relevant_chunks", []) if "content" in c]
        except Exception as e:
            failures += 1
            tqdm.write(f"  [FAIL #{failures}] Q{i + 1}: {type(e).__name__}: {str(e)[:120]}")
            answer = ""
            chunks = []

        answers.append(answer)
        contexts.append(chunks if chunks else [""])

    pbar.close()
    if failures:
        logger.warning(f"{failures}/{len(samples)} questions failed during pipeline run")
    return answers, contexts


def run_ragas_evaluation(
    questions: list[str],
    answers: list[str],
    contexts: list[list[str]],
    ground_truths: list[str],
) -> dict:
    """Run RAGAS evaluation and return metric scores.

    RAGAS 0.2+ uses SingleTurnSample with field names user_input / response /
    retrieved_contexts / reference (the old question/answer/contexts/ground_truth
    names were deprecated).
    """
    from ragas import EvaluationDataset, SingleTurnSample, evaluate
    from ragas.llms import llm_factory
    from ragas.metrics import AnswerRelevancy, ContextPrecision, ContextRecall, Faithfulness

    logger.info(f"Initializing RAGAS with evaluator model: {EVALUATOR_MODEL}")
    evaluator_llm = llm_factory(EVALUATOR_MODEL)

    metrics = [
        Faithfulness(llm=evaluator_llm),
        AnswerRelevancy(llm=evaluator_llm),
        ContextPrecision(llm=evaluator_llm),
        ContextRecall(llm=evaluator_llm),
    ]

    samples = [
        SingleTurnSample(
            user_input=q,
            response=a,
            retrieved_contexts=c,
            reference=gt,
        )
        for q, a, c, gt in zip(questions, answers, contexts, ground_truths)
    ]
    dataset = EvaluationDataset(samples=samples)

    logger.info(f"Running RAGAS evaluation on {len(questions)} samples (this runs its own LLM-as-judge calls)...")
    results = evaluate(dataset=dataset, metrics=metrics, show_progress=True)

    # RAGAS returns per-metric means accessible via df().mean() or the Result mapping
    df = results.to_pandas()
    scores = {
        "faithfulness": float(df["faithfulness"].mean()),
        "answer_relevancy": float(df["answer_relevancy"].mean()),
        "context_precision": float(df["context_precision"].mean()),
        "context_recall": float(df["context_recall"].mean()),
    }
    return scores


def check_thresholds(scores: dict) -> bool:
    """Check if scores meet CI gate thresholds. Returns True if all pass."""
    all_pass = True

    logger.info("--- CI Gate Threshold Check ---")
    for metric, threshold in THRESHOLDS.items():
        score = scores.get(metric, 0.0)
        passed = score >= threshold
        status = "PASS" if passed else "FAIL"
        logger.info(f"  {metric}: {score:.4f} (threshold: {threshold}) [{status}]")
        if not passed:
            all_pass = False

    logger.info("--- Informational Metrics ---")
    for metric, threshold in INFORMATIONAL_THRESHOLDS.items():
        score = scores.get(metric, 0.0)
        status = "OK" if score >= threshold else "BELOW TARGET"
        logger.info(f"  {metric}: {score:.4f} (target: {threshold}) [{status}]")

    return all_pass


def _intermediate_path(output_path: Path | None) -> Path:
    """Return a sibling path for intermediate pipeline outputs (answers + contexts)."""
    if output_path:
        return output_path.with_suffix(".pipeline.json")
    return Path(__file__).parent / "eval_results" / "pipeline_cache.json"


def main():
    parser = argparse.ArgumentParser(description="Run RAGAS evaluation on the RAG Agent pipeline")
    parser.add_argument("--output", "-o", type=str, default=None, help="Output JSON file path for results")
    parser.add_argument("--check-thresholds", action="store_true", help="Exit non-zero if metrics below CI thresholds")
    parser.add_argument(
        "--skip-pipeline",
        action="store_true",
        help="Load pipeline answers/contexts from intermediate cache (skip re-running graph)",
    )
    args = parser.parse_args()

    output_path = Path(args.output) if args.output else None
    intermediate_path = _intermediate_path(output_path)

    # Load dataset
    samples = load_dataset(DATASET_PATH)
    questions = [s["question"] for s in samples]
    ground_truths = [s["ground_truth"] for s in samples]

    # --- Pipeline phase (or load cached) ---
    if args.skip_pipeline:
        if not intermediate_path.exists():
            logger.error(f"--skip-pipeline set but no cache at {intermediate_path}")
            sys.exit(1)
        logger.info(f"Loading cached pipeline outputs from {intermediate_path}")
        with open(intermediate_path) as f:
            cache = json.load(f)
        answers = cache["answers"]
        contexts = cache["contexts"]
        pipeline_time = cache.get("pipeline_time_seconds", 0.0)
    else:
        logger.info("Building RAG graph...")
        graph = build_graph(checkpointer=None)

        start = time.time()
        answers, contexts = run_rag_pipeline(graph, samples)
        pipeline_time = time.time() - start
        logger.info(f"Pipeline completed in {pipeline_time:.1f}s ({pipeline_time / len(samples):.1f}s per question)")

        # Persist intermediate results immediately so a RAGAS failure doesn't force a re-run
        intermediate_path.parent.mkdir(parents=True, exist_ok=True)
        with open(intermediate_path, "w") as f:
            json.dump(
                {"questions": questions, "answers": answers, "contexts": contexts, "pipeline_time_seconds": round(pipeline_time, 1)},
                f,
                indent=2,
            )
        logger.info(f"Intermediate pipeline results cached to {intermediate_path}")
        logger.info("If RAGAS scoring fails, re-run with --skip-pipeline to reuse these answers")

    # --- RAGAS scoring phase ---
    start = time.time()
    scores = run_ragas_evaluation(questions, answers, contexts, ground_truths)
    eval_time = time.time() - start
    logger.info(f"RAGAS evaluation completed in {eval_time:.1f}s")

    # Build output
    output = {
        "scores": scores,
        "thresholds": THRESHOLDS,
        "num_samples": len(samples),
        "pipeline_time_seconds": round(pipeline_time, 1),
        "eval_time_seconds": round(eval_time, 1),
        "evaluator_model": EVALUATOR_MODEL,
    }

    # Print summary
    logger.info("=== RAGAS Evaluation Results ===")
    for metric, score in scores.items():
        logger.info(f"  {metric}: {score:.4f}")

    # Save output
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(output, f, indent=2)
        logger.info(f"Results saved to {output_path}")

    # Threshold check
    if args.check_thresholds:
        if check_thresholds(scores):
            logger.info("All CI gate thresholds passed!")
            sys.exit(0)
        else:
            logger.error("CI gate thresholds NOT met — failing.")
            sys.exit(1)


if __name__ == "__main__":
    main()
