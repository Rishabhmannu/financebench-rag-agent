"""Deterministic gold-chunk labeling for the FinanceBench-150 phase eval.

Two-phase: token-trigram recall over all chunks in the doc; if a span fails
the trigram thresholds, fall back to unigram (bag-of-words) recall over the
single page predicted by the measured +1 offset between FinanceBench's
evidence_page_num and our chunker's page_number. Order-invariant unigram
matching recovers questions whose evidence is a financial table that the
chunker rendered as pipe-markdown.

Outputs (overwritten each run):
  tests/evaluation/phase_eval_data/v1/gold_chunks.jsonl
  tests/evaluation/phase_eval_data/v1/_audit.jsonl
"""

import argparse
import json
import re
import time
from collections import Counter
from pathlib import Path
from typing import Callable, Iterable

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from src.config.settings import settings

FB_JSONL = Path("data/raw/financebench/financebench_open_source.jsonl")
OUTPUT_DIR = Path("tests/evaluation/phase_eval_data/v1")
GOLD_JSONL = OUTPUT_DIR / "gold_chunks.jsonl"
AUDIT_JSONL = OUTPUT_DIR / "_audit.jsonl"
COLLECTION = "financebench_corpus_pypdf_voyage_finance2"

SHINGLE_N = 3
PRIMARY_RECALL = 0.70
MULTI_PIECE_RECALL = 0.10
MULTI_COMBINED_RECALL = 0.90
MAX_MULTI_CHUNKS = 6
UNIGRAM_PAGE_OFFSET = 1
UNIGRAM_PRIMARY_RECALL = 0.70
UNIGRAM_COMBINED_RECALL = 0.90
TOP_AUDIT_CANDIDATES = 5
SCROLL_BATCH = 500

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_PREFIX_RE = re.compile(r"^\s*\[[^\]]*\]\s*")


def tokenize(text: str) -> list[str]:
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())


def strip_prefix(content: str) -> str:
    return _PREFIX_RE.sub("", content or "", count=1)


def trigrams(tokens: list[str]) -> Counter:
    if len(tokens) < SHINGLE_N:
        return Counter(tokens)
    return Counter(tuple(tokens[i : i + SHINGLE_N]) for i in range(len(tokens) - SHINGLE_N + 1))


def unigrams(tokens: list[str]) -> Counter:
    return Counter(tokens)


def overlap_metrics(ev: Counter, chunk: Counter) -> tuple[float, float, float]:
    if not ev:
        return 0.0, 0.0, 0.0
    matched = sum(min(c, chunk.get(s, 0)) for s, c in ev.items())
    ev_total = sum(ev.values())
    ch_total = sum(chunk.values())
    recall = matched / ev_total
    precision = matched / ch_total if ch_total else 0.0
    union = ev_total + ch_total - matched
    iou = matched / union if union else 0.0
    return recall, precision, iou


def fetch_doc_chunks(client: QdrantClient, doc_name: str) -> list[dict]:
    flt = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="financebench_doc_name",
                match=qmodels.MatchValue(value=doc_name),
            )
        ]
    )
    out: list[dict] = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=COLLECTION,
            scroll_filter=flt,
            limit=SCROLL_BATCH,
            with_payload=True,
            with_vectors=False,
            offset=offset,
        )
        out.extend({"id": p.id, "payload": p.payload} for p in points)
        if offset is None:
            break
    return out


def _chunk_record(p: dict) -> dict:
    pl = p["payload"]
    return {
        "qdrant_id": p["id"],
        "source_file": pl.get("source_file"),
        "chunk_index": pl.get("chunk_index"),
        "page_number": pl.get("page_number"),
        "section_header": pl.get("section_header") or "",
        "chunk_type": pl.get("chunk_type", "text"),
    }


def _match_phase(
    ev_counter: Counter,
    candidate_chunks: list[dict],
    sh_fn: Callable[[dict], Counter],
    primary: float,
    piece: float,
    combined: float,
    max_multi: int,
) -> tuple[list[dict], list[tuple[dict, float, float, float]], str]:
    """Score every candidate against ev_counter, return (selected, scored_sorted, method)."""
    if not ev_counter or not candidate_chunks:
        return [], [], "no_match_top0pct"

    scored: list[tuple[dict, float, float, float]] = []
    for p in candidate_chunks:
        r, pr, iou = overlap_metrics(ev_counter, sh_fn(p))
        scored.append((p, r, pr, iou))
    scored.sort(key=lambda x: x[1], reverse=True)

    top_recall = scored[0][1]

    if top_recall >= primary:
        selected = [p for p, r, _, _ in scored if r >= primary]
        return selected, scored, f"single_{int(top_recall * 100)}pct"

    # Multi-chunk greedy union until combined recall >= combined threshold
    covered: Counter = Counter()
    ev_total = sum(ev_counter.values())
    chosen: list[dict] = []
    for p, r, _, _ in scored:
        if r < piece:
            break
        sh = sh_fn(p)
        for s, c in ev_counter.items():
            already = covered[s]
            remaining = c - already
            if remaining > 0:
                covered[s] = already + min(remaining, sh.get(s, 0))
        chosen.append(p)
        if ev_total and sum(covered.values()) / ev_total >= combined:
            break
        if len(chosen) >= max_multi:
            break

    combined_recall = sum(covered.values()) / ev_total if ev_total else 0.0
    if combined_recall >= combined and chosen:
        return chosen, scored, f"multi_{len(chosen)}chunks_{int(combined_recall * 100)}pct"
    return [], scored, f"no_match_top{int(top_recall * 100)}pct"


def label_one(
    fb_record: dict,
    doc_chunks: list[dict],
    primary_recall: float,
    piece_recall: float,
    combined_recall: float,
    max_multi: int,
    unigram_page_offset: int,
    unigram_primary: float,
    unigram_combined: float,
) -> tuple[dict, list[dict]]:
    fb_id = fb_record["financebench_id"]
    doc_name = fb_record["doc_name"]
    evidence = fb_record.get("evidence") or []

    tri_cache: dict[str, Counter] = {}
    uni_cache: dict[str, Counter] = {}

    def tri_sh(p: dict) -> Counter:
        pid = p["id"]
        if pid not in tri_cache:
            tri_cache[pid] = trigrams(tokenize(strip_prefix(p["payload"].get("content", ""))))
        return tri_cache[pid]

    def uni_sh(p: dict) -> Counter:
        pid = p["id"]
        if pid not in uni_cache:
            uni_cache[pid] = unigrams(tokenize(strip_prefix(p["payload"].get("content", ""))))
        return uni_cache[pid]

    selected: list[dict] = []
    seen_ids: set[str] = set()
    fb_ev_dump: list[dict] = []
    methods: list[str] = []
    max_recalls: list[float] = []
    audit: list[dict] = []

    def add_audit(scored, ev_idx, ev_page, phase, sel_ids):
        for rank, (p, r, pr, iou) in enumerate(scored[:TOP_AUDIT_CANDIDATES], start=1):
            audit.append(
                {
                    "financebench_id": fb_id,
                    "doc_name": doc_name,
                    "evidence_idx": ev_idx,
                    "candidate_rank": rank,
                    "qdrant_id": p["id"],
                    "chunk_index": p["payload"].get("chunk_index"),
                    "page_number": p["payload"].get("page_number"),
                    "evidence_page_num": ev_page,
                    "page_match": (
                        p["payload"].get("page_number") == ev_page
                        if ev_page is not None
                        else None
                    ),
                    "recall": round(r, 4),
                    "precision": round(pr, 4),
                    "iou": round(iou, 4),
                    "selected": p["id"] in sel_ids,
                    "phase": phase,
                }
            )

    for ev_idx, ev in enumerate(evidence):
        ev_text = ev.get("evidence_text") or ""
        ev_page = ev.get("evidence_page_num")
        fb_ev_dump.append(
            {
                "evidence_page_num": ev_page,
                "evidence_text_preview": ev_text[:200].replace("\n", " "),
            }
        )

        ev_tokens = tokenize(ev_text)
        ev_tri = trigrams(ev_tokens)
        if not ev_tri:
            methods.append("empty_evidence")
            max_recalls.append(0.0)
            continue

        # Phase 1 — trigram over all chunks in the doc
        sel_tri, scored_tri, method_tri = _match_phase(
            ev_tri, doc_chunks, tri_sh,
            primary_recall, piece_recall, combined_recall, max_multi,
        )
        max_recalls.append(scored_tri[0][1] if scored_tri else 0.0)
        sel_tri_ids = {p["id"] for p in sel_tri}
        add_audit(scored_tri, ev_idx, ev_page, "trigram", sel_tri_ids)

        method_for_span = method_tri
        selected_for_span: list[dict] = list(sel_tri)

        # Phase 2 — unigram-on-validated-page fallback for trigram no_match
        if method_tri.startswith("no_match") and ev_page is not None:
            target_page = ev_page + unigram_page_offset
            page_chunks = [
                p for p in doc_chunks if p["payload"].get("page_number") == target_page
            ]
            if page_chunks:
                ev_uni = unigrams(ev_tokens)
                sel_uni, scored_uni, method_uni = _match_phase(
                    ev_uni, page_chunks, uni_sh,
                    unigram_primary, piece_recall, unigram_combined, max_multi,
                )
                sel_uni_ids = {p["id"] for p in sel_uni}
                add_audit(scored_uni, ev_idx, ev_page, "unigram_fallback", sel_uni_ids)

                if not method_uni.startswith("no_match"):
                    method_for_span = f"unigram_page_{method_uni}"
                    selected_for_span = list(sel_uni)

        for p in selected_for_span:
            if p["id"] not in seen_ids:
                seen_ids.add(p["id"])
                selected.append(_chunk_record(p))
        methods.append(method_for_span)

    gold_record = {
        "financebench_id": fb_id,
        "doc_name": doc_name,
        "company": fb_record.get("company"),
        "question_type": fb_record.get("question_type", ""),
        "fb_evidence": fb_ev_dump,
        "gold_chunks": selected,
        "labeling_method": "+".join(methods) if methods else "no_evidence",
        "max_single_recall_per_span": [round(x, 4) for x in max_recalls],
        "n_chunks_in_doc": len(doc_chunks),
    }
    return gold_record, audit


def iter_fb() -> Iterable[dict]:
    with open(FB_JSONL) as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="Stop after N questions (pilot mode).")
    parser.add_argument("--only", type=str, default=None,
                        help="Comma-list of financebench_ids only.")
    parser.add_argument("--primary-recall", type=float, default=PRIMARY_RECALL)
    parser.add_argument("--piece-recall", type=float, default=MULTI_PIECE_RECALL)
    parser.add_argument("--combined-recall", type=float, default=MULTI_COMBINED_RECALL)
    parser.add_argument("--max-multi", type=int, default=MAX_MULTI_CHUNKS)
    parser.add_argument("--unigram-page-offset", type=int, default=UNIGRAM_PAGE_OFFSET)
    parser.add_argument("--unigram-primary-recall", type=float, default=UNIGRAM_PRIMARY_RECALL)
    parser.add_argument("--unigram-combined-recall", type=float, default=UNIGRAM_COMBINED_RECALL)
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    only_ids = set(args.only.split(",")) if args.only else None

    client = QdrantClient(
        host=settings.QDRANT_HOST, port=settings.QDRANT_PORT, timeout=30.0
    )
    info = client.get_collection(COLLECTION)
    print(
        f"preflight: collection={COLLECTION} points={info.points_count}\n"
        f"  trigram: primary={args.primary_recall} piece={args.piece_recall} "
        f"combined={args.combined_recall} max_multi={args.max_multi}\n"
        f"  unigram fallback: page_offset=+{args.unigram_page_offset} "
        f"primary={args.unigram_primary_recall} combined={args.unigram_combined_recall}"
    )

    doc_cache: dict[str, list[dict]] = {}
    stats = {
        "questions_total": 0,
        "single_chunk_trigram": 0,
        "multi_chunk_trigram": 0,
        "single_chunk_unigram_page": 0,
        "multi_chunk_unigram_page": 0,
        "no_match": 0,
        "spans_total": 0,
        "spans_no_match": 0,
    }
    recall_hist = [0] * 11

    t0 = time.time()
    with open(GOLD_JSONL, "w") as gold_out, open(AUDIT_JSONL, "w") as audit_out:
        for rec in iter_fb():
            fb_id = rec["financebench_id"]
            if only_ids is not None and fb_id not in only_ids:
                continue
            stats["questions_total"] += 1
            doc_name = rec["doc_name"]
            if doc_name not in doc_cache:
                doc_cache[doc_name] = fetch_doc_chunks(client, doc_name)
            chunks = doc_cache[doc_name]
            if not chunks:
                print(f"  WARN: 0 chunks for doc {doc_name} — skipping {fb_id}")
                continue

            gold, audit = label_one(
                rec, chunks,
                args.primary_recall, args.piece_recall,
                args.combined_recall, args.max_multi,
                args.unigram_page_offset, args.unigram_primary_recall,
                args.unigram_combined_recall,
            )
            stats["spans_total"] += len(rec.get("evidence") or [])
            method_parts = gold["labeling_method"].split("+")
            stats["spans_no_match"] += sum(1 for m in method_parts if m.startswith("no_match"))

            if all(m.startswith("no_match") or m == "empty_evidence" for m in method_parts):
                stats["no_match"] += 1
            elif any(m.startswith("unigram_page_multi_") for m in method_parts):
                stats["multi_chunk_unigram_page"] += 1
            elif any(m.startswith("unigram_page_single_") for m in method_parts):
                stats["single_chunk_unigram_page"] += 1
            elif any(m.startswith("multi_") for m in method_parts):
                stats["multi_chunk_trigram"] += 1
            else:
                stats["single_chunk_trigram"] += 1

            for r in gold["max_single_recall_per_span"]:
                bucket = min(int(r * 10), 10)
                recall_hist[bucket] += 1

            gold_out.write(json.dumps(gold) + "\n")
            for a in audit:
                audit_out.write(json.dumps(a) + "\n")

            if args.limit and stats["questions_total"] >= args.limit:
                break

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s. Stats:")
    for k, v in stats.items():
        print(f"  {k:32s} {v}")
    print(f"\ntrigram max_single_recall histogram (per evidence span):")
    for i, c in enumerate(recall_hist):
        lo, hi = i * 0.1, (i + 1) * 0.1
        label = f"{lo:.1f}–{hi:.1f}" if i < 10 else "1.0"
        bar = "#" * min(c, 60)
        print(f"  {label:7s} {c:4d}  {bar}")


if __name__ == "__main__":
    main()
