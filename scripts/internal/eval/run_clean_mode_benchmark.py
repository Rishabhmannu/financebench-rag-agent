"""Run clean benchmark modes end-to-end: ingestion -> pipeline -> RAGAS + Patronus.

Modes:
  - docling_only: force Docling parsing/chunking path
  - pypdf_only: bypass Docling via SKIP_DOCLING=1
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str], env: dict[str, str]) -> None:
    print(f"\n$ {' '.join(cmd)}")
    subprocess.run(cmd, check=True, env=env)


def _resolve_python_executable(preferred_env_name: str = "agentic-ai") -> str:
    """Pick a Python interpreter that has project deps installed.

    Priority:
      1) BENCHMARK_PYTHON env override (explicit)
      2) current shell `python` if already in preferred conda env
      3) common conda path for preferred env
      4) current interpreter (sys.executable)
    """
    explicit = os.environ.get("BENCHMARK_PYTHON")
    if explicit:
        return explicit

    conda_env = os.environ.get("CONDA_DEFAULT_ENV", "")
    shell_python = shutil.which("python")
    if conda_env == preferred_env_name and shell_python:
        return shell_python

    common_conda = Path(f"/opt/anaconda3/envs/{preferred_env_name}/bin/python")
    if common_conda.exists():
        return str(common_conda)

    return sys.executable


def main() -> None:
    parser = argparse.ArgumentParser(description="Run clean ingestion+eval benchmark mode")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["docling_only", "pypdf_only"],
        help="Benchmark mode to run",
    )
    parser.add_argument("--collection", default=None, help="Qdrant collection name override")
    parser.add_argument("--output", required=True, help="Output JSON path for run_financebench.py")
    parser.add_argument("--limit", type=int, default=None, help="Optional subset limit for smoke runs")
    parser.add_argument(
        "--force-ingestion",
        action="store_true",
        help="Force re-ingestion even if docs already exist in collection",
    )
    parser.add_argument(
        "--resume-eval",
        action="store_true",
        help="If pipeline cache exists for output, skip pipeline and only re-score",
    )
    parser.add_argument(
        "--ragas-judge-model",
        default=os.environ.get("RAGAS_EVALUATOR_MODEL", "gpt-4o-mini"),
        help="RAGAS judge model id for FinanceBench runner",
    )
    parser.add_argument(
        "--patronus-evaluator",
        default="judge",
        help="Patronus evaluator alias (judge/judge-small/judge-large)",
    )
    parser.add_argument(
        "--patronus-criteria",
        default="patronus:fuzzy-match",
        help="Patronus criteria/evaluator id",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    python = _resolve_python_executable()
    print(f"[runner] using python: {python}")

    if args.mode == "docling_only":
        env.pop("SKIP_DOCLING", None)
        ingest_script = root / "scripts" / "ingest_financebench_docling.py"
        collection = args.collection or "financebench_corpus_docling_clean"
    else:
        env["SKIP_DOCLING"] = "1"
        ingest_script = root / "scripts" / "ingest_financebench.py"
        collection = args.collection or "financebench_corpus_pypdf_clean"

    ingest_cmd = [python, str(ingest_script), "--collection", collection]
    if args.limit:
        ingest_cmd.extend(["--limit", str(args.limit)])
    if args.force_ingestion:
        ingest_cmd.append("--force")
    _run(ingest_cmd, env)

    eval_cmd = [
        python,
        str(root / "tests" / "evaluation" / "run_financebench.py"),
        "--collection",
        collection,
        "--output",
        args.output,
        "--ragas-judge-model",
        args.ragas_judge_model,
        "--patronus-evaluator",
        args.patronus_evaluator,
        "--patronus-criteria",
        args.patronus_criteria,
    ]
    if args.limit:
        eval_cmd.extend(["--limit", str(args.limit)])
    if args.resume_eval:
        cache_path = Path(args.output).with_suffix(".pipeline.json")
        if cache_path.exists():
            eval_cmd.append("--skip-pipeline")
            print(f"[resume-eval] Using existing pipeline cache: {cache_path}")
    _run(eval_cmd, env)

    print("\nDone.")
    print(f"mode={args.mode}")
    print(f"collection={collection}")
    print(f"output={args.output}")


if __name__ == "__main__":
    main()

