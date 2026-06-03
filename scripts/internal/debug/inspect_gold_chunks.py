"""Spot-check helper for the FinanceBench gold-chunk labels.

Reads gold_chunks.jsonl + _audit.jsonl, samples a stratified subset, and
prints each Q with its evidence preview alongside the selected chunk(s)
content excerpt and overlap scores. Use this to manually verify that the
deterministic labeler's threshold is producing correct gold sets.
"""

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path

from qdrant_client import QdrantClient

from src.config.settings import settings

DEFAULTS_DIR = Path("tests/evaluation/phase_eval_data/v1")
COLLECTION = "financebench_corpus_pypdf_voyage_finance2"
CONTENT_EXCERPT_CHARS = 600


def load_jsonl(path: Path) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def stratified_sample(gold: list[dict], n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in gold:
        method = r["labeling_method"]
        if any(m.startswith("no_match") for m in method.split("+")):
            key = "no_match"
        elif any(m.startswith("multi_") for m in method.split("+")):
            key = "multi_chunk"
        elif r.get("question_type") == "metrics-generated":
            key = "single_metrics"
        else:
            key = "single_other"
        buckets[key].append(r)

    per_bucket = max(1, n // max(1, len(buckets)))
    sampled: list[dict] = []
    for key, items in buckets.items():
        rng.shuffle(items)
        sampled.extend(items[:per_bucket])
    rng.shuffle(sampled)
    return sampled[:n]


def fetch_chunk_content(client: QdrantClient, qdrant_ids: list[str]) -> dict[str, str]:
    if not qdrant_ids:
        return {}
    points = client.retrieve(
        collection_name=COLLECTION,
        ids=qdrant_ids,
        with_payload=True,
        with_vectors=False,
    )
    return {p.id: p.payload.get("content", "") for p in points}


def fmt_record(rec: dict, audit_index: dict, content_by_id: dict[str, str]) -> str:
    out = []
    out.append("=" * 78)
    out.append(
        f"{rec['financebench_id']}  |  {rec['company']}  |  doc={rec['doc_name']}  "
        f"|  type={rec.get('question_type')}"
    )
    out.append(f"labeling_method: {rec['labeling_method']}")
    out.append(f"max_single_recall_per_span: {rec['max_single_recall_per_span']}")
    out.append("")
    for ev_idx, ev in enumerate(rec["fb_evidence"]):
        out.append(f"  evidence_span[{ev_idx}] page={ev['evidence_page_num']}")
        out.append(f"    {ev['evidence_text_preview']}{'...' if len(ev['evidence_text_preview']) >= 200 else ''}")
        audit_for_span = sorted(
            [
                a
                for a in audit_index.get(rec["financebench_id"], [])
                if a["evidence_idx"] == ev_idx
            ],
            key=lambda a: a["candidate_rank"],
        )
        for a in audit_for_span:
            star = "*" if a["selected"] else " "
            pm = "" if a["page_match"] is None else f" page_match={a['page_match']}"
            out.append(
                f"    {star} rank={a['candidate_rank']} "
                f"chunk_idx={a['chunk_index']} page={a['page_number']}{pm} "
                f"recall={a['recall']:.3f} prec={a['precision']:.3f} iou={a['iou']:.3f}"
            )
            if a["selected"]:
                content = content_by_id.get(a["qdrant_id"], "")
                excerpt = content[:CONTENT_EXCERPT_CHARS].replace("\n", " ")
                suffix = "..." if len(content) > CONTENT_EXCERPT_CHARS else ""
                out.append(f"        chunk: {excerpt}{suffix}")
    out.append("")
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gold", type=Path, default=DEFAULTS_DIR / "gold_chunks.jsonl")
    parser.add_argument("--audit", type=Path, default=DEFAULTS_DIR / "_audit.jsonl")
    parser.add_argument("--sample", type=int, default=15)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--only", type=str, default=None,
                        help="Comma-list of financebench_ids to inspect (overrides --sample).")
    args = parser.parse_args()

    gold_records = load_jsonl(args.gold)
    audit_records = load_jsonl(args.audit)

    audit_index: dict[str, list[dict]] = defaultdict(list)
    for a in audit_records:
        audit_index[a["financebench_id"]].append(a)

    if args.only:
        only_ids = set(args.only.split(","))
        picked = [r for r in gold_records if r["financebench_id"] in only_ids]
    else:
        picked = stratified_sample(gold_records, args.sample, args.seed)

    needed_ids: list[str] = []
    for r in picked:
        for c in r["gold_chunks"]:
            needed_ids.append(c["qdrant_id"])

    client = QdrantClient(
        host=settings.QDRANT_HOST, port=settings.QDRANT_PORT, timeout=30.0
    )
    content_by_id = fetch_chunk_content(client, needed_ids)

    print(f"sampled {len(picked)} of {len(gold_records)} labeled questions\n")
    for r in picked:
        print(fmt_record(r, audit_index, content_by_id))

    print("\nWhen reviewing each entry: confirm the starred chunk(s) actually "
          "contain the evidence text. Note any disagreements; we'll re-threshold "
          "if disagreement rate > 1/15.")


if __name__ == "__main__":
    main()
