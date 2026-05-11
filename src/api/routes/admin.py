"""Admin-only endpoints for operational visibility.

Sprint 8 8d: `/admin/costs` aggregates LLM spend from the self-hosted
Langfuse instance over a configurable time window, broken down by user,
model, and trace name. Querying Langfuse keeps the rag-agent itself
stateless on the cost-tracking side — the proxy is the source of truth,
this endpoint is just a thin reader on top.

Sprint 9.0: extends to `/admin/users` and `/admin/roles` CRUD so the
frontend admin panel can render the user table and edit RBAC dynamically.
"""
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from src.api.dependencies import get_current_user
from src.config.settings import settings
from src.models.auth import User
from src.models.schemas import Role, RoleCreate, RoleUpdate
from src.services import roles_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )


async def _fetch_observations(
    *, start_iso: str, end_iso: str, limit: int = 100
) -> list[dict]:
    """Page through Langfuse's GENERATION observations within the window.

    Langfuse paginates at ~50 items/page; we keep walking until the response
    page is short. The endpoint is anonymous-bearer-auth via basic auth on
    the project public/secret key pair.
    """
    auth = (settings.LANGFUSE_PUBLIC_KEY, settings.LANGFUSE_SECRET_KEY)
    url = f"{settings.LANGFUSE_HOST.rstrip('/')}/api/public/observations"
    out: list[dict] = []
    page = 1
    async with httpx.AsyncClient(timeout=20.0) as client:
        while True:
            resp = await client.get(
                url,
                auth=auth,
                params={
                    "type": "GENERATION",
                    "fromStartTime": start_iso,
                    "toStartTime": end_iso,
                    "limit": limit,
                    "page": page,
                },
            )
            if resp.status_code != 200:
                logger.error("Langfuse query failed: %s %s", resp.status_code, resp.text[:300])
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Failed to query Langfuse for cost data",
                )
            payload = resp.json()
            page_data = payload.get("data", [])
            out.extend(page_data)
            if len(page_data) < limit:
                break
            page += 1
            if page > 100:
                # Safety belt — 10k generations is plenty for an admin view
                logger.warning("Aborting Langfuse pagination at page 100")
                break
    return out


async def _fetch_trace_user_map(trace_ids: set[str]) -> dict[str, str | None]:
    """Resolve trace_id → userId for a batch of traces. Langfuse stores the
    user attribution at the trace level, not on individual generations,
    which is why this second hop is needed.
    """
    if not trace_ids:
        return {}
    auth = (settings.LANGFUSE_PUBLIC_KEY, settings.LANGFUSE_SECRET_KEY)
    base_url = f"{settings.LANGFUSE_HOST.rstrip('/')}/api/public/traces"
    out: dict[str, str | None] = {}
    async with httpx.AsyncClient(timeout=20.0) as client:
        # Langfuse's traces endpoint doesn't support an `ids[]` filter, so we
        # fetch each one. With ~100s of traces over a typical window this is
        # cheap; the scale at which it bites we'd batch-cache via Redis.
        for tid in trace_ids:
            try:
                resp = await client.get(f"{base_url}/{tid}", auth=auth)
                if resp.status_code == 200:
                    out[tid] = resp.json().get("userId")
                else:
                    out[tid] = None
            except httpx.HTTPError:
                out[tid] = None
    return out


@router.get("/users")
async def admin_users(user: User = Depends(get_current_user)):
    """List configured users. Admin only.

    Sprint 9 frontend "Users" tab consumes this alongside `/admin/costs`
    to render the per-user spend table. Reads from the in-memory DEV_USERS
    dict for now; swap implementation when a real user store lands. The
    response shape stays compatible with either backing store.
    """
    _require_admin(user)
    from src.api.routes.auth import DEV_USERS

    return {
        "users": [
            {
                "username": uname,
                "name": info["name"],
                "role": info["role"],
                "department": info["department"],
            }
            for uname, info in DEV_USERS.items()
        ]
    }


@router.get("/costs")
async def admin_costs(
    days: int = Query(7, ge=1, le=90, description="Days back from now"),
    user: User = Depends(get_current_user),
):
    """Aggregate LLM cost from Langfuse for the last `days` days.

    Returns three groupings: by `userId` (Sprint 8 8d's per-user attribution
    surfaces here once chat traffic flows through the auth dependency),
    by model name, and by trace name (e.g. `litellm-acompletion` vs
    `litellm-aembedding`). All cost figures are in USD.
    """
    _require_admin(user)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    start_iso = start.isoformat()
    end_iso = end.isoformat()

    observations = await _fetch_observations(start_iso=start_iso, end_iso=end_iso)

    by_user: dict[str | None, dict] = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0, "tokens": 0})
    by_model: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0, "tokens": 0})
    by_trace_name: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost_usd": 0.0, "tokens": 0})
    total = {"calls": 0, "cost_usd": 0.0, "tokens": 0}

    trace_ids = {o.get("traceId") for o in observations if o.get("traceId")}
    trace_user_map = await _fetch_trace_user_map(trace_ids)

    for o in observations:
        cost = float(o.get("calculatedTotalCost") or 0.0)
        ptok = int(o.get("promptTokens") or 0)
        ctok = int(o.get("completionTokens") or 0)
        tokens = ptok + ctok
        model = o.get("model") or "unknown"
        trace_id = o.get("traceId")
        uid = trace_user_map.get(trace_id) if trace_id else None
        trace_name = o.get("name") or "unknown"  # e.g. "litellm-acompletion", "litellm-aembedding"

        total["calls"] += 1
        total["cost_usd"] += cost
        total["tokens"] += tokens

        for bucket, key in ((by_user, uid), (by_model, model), (by_trace_name, trace_name)):
            b = bucket[key]
            b["calls"] += 1
            b["cost_usd"] += cost
            b["tokens"] += tokens

    def _to_list(d: dict) -> list[dict]:
        out = [{"key": k, **v} for k, v in d.items()]
        return sorted(out, key=lambda x: x["cost_usd"], reverse=True)

    return {
        "window_days": days,
        "start": start_iso,
        "end": end_iso,
        "total": total,
        "by_user": _to_list(by_user),
        "by_model": _to_list(by_model),
        "by_trace_name": _to_list(by_trace_name),
    }


# ── /admin/roles — dynamic RBAC role CRUD (Sprint 9.0) ──────────────────

@router.get("/roles")
async def list_roles_endpoint(user: User = Depends(get_current_user)):
    _require_admin(user)
    return {"roles": roles_service.list_roles()}


@router.post("/roles", status_code=status.HTTP_201_CREATED, response_model=Role)
async def create_role_endpoint(role: RoleCreate, user: User = Depends(get_current_user)):
    _require_admin(user)
    if roles_service.get_role(role.name) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Role '{role.name}' already exists")
    created = roles_service.create_role(role.model_dump())
    return created


@router.patch("/roles/{name}", response_model=Role)
async def update_role_endpoint(name: str, patch: RoleUpdate, user: User = Depends(get_current_user)):
    _require_admin(user)
    existing = roles_service.get_role(name)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Role '{name}' not found")
    # System roles can have their permissions edited but the slug stays;
    # blocking patches on them would be too restrictive for operators.
    patch_dict = {k: v for k, v in patch.model_dump().items() if v is not None}
    updated = roles_service.update_role(name, patch_dict)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Role '{name}' disappeared during update")
    return updated


@router.delete("/roles/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role_endpoint(name: str, user: User = Depends(get_current_user)):
    _require_admin(user)
    outcome = roles_service.delete_role(name)
    if outcome == "not_found":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Role '{name}' not found")
    if outcome == "blocked_system":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Role '{name}' is a system role and cannot be deleted (only its permissions can be edited)",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── /admin/audit — recent queries timeline (Sprint 9.0.2) ───────────────

@router.get("/audit")
async def admin_audit(
    hours: int = Query(24, ge=1, le=720, description="Look-back window in hours"),
    limit: int = Query(100, ge=1, le=500, description="Max events returned"),
    user: User = Depends(get_current_user),
):
    """Derive a recent-queries timeline from Langfuse traces.

    Each `chat` request lands a top-level Langfuse trace with the user
    message + assistant response captured in the input/output blobs. We
    fetch the most-recent traces from Langfuse's public `/api/public/traces`
    endpoint, filter to within the time window, and surface:
        - timestamp
        - user_id (from the trace's `userId`, populated by Sprint 8 8d's
          attribution path through LiteLLM)
        - the user's query (best-effort extracted from the trace's input)
        - the model that handled the chat (best-effort from observations)
        - cost (sum of generation calculatedTotalCost on the trace)

    Frontend renders this as the "Activity" tab. Without this endpoint the
    panel would have to roll-up /admin/costs traces client-side to derive
    the same info — uglier and chattier.
    """
    _require_admin(user)

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    auth = (settings.LANGFUSE_PUBLIC_KEY, settings.LANGFUSE_SECRET_KEY)

    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            r = await client.get(
                f"{settings.LANGFUSE_HOST.rstrip('/')}/api/public/traces",
                auth=auth,
                params={
                    "fromTimestamp": start.isoformat(),
                    "toTimestamp": end.isoformat(),
                    "limit": min(limit, 100),  # Langfuse caps page size
                },
            )
        except httpx.HTTPError as e:
            logger.error("Langfuse traces query failed: %s", e)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to query Langfuse for audit data",
            )
        if r.status_code != 200:
            logger.error("Langfuse traces query non-200: %s %s", r.status_code, r.text[:200])
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to query Langfuse for audit data",
            )
        payload = r.json().get("data", [])

    events: list[dict] = []
    for t in payload:
        # Trace input/output can be a dict, list, or string depending on
        # the call shape — best-effort extract a user query preview.
        query_preview = _trace_input_preview(t.get("input"))
        # Model: peek at the first observation's name; cheap and good enough
        model_hint = (t.get("name") or "").replace("litellm-", "")
        events.append({
            "trace_id": t.get("id"),
            "timestamp": t.get("timestamp") or t.get("createdAt"),
            "user_id": t.get("userId"),
            "query": query_preview,
            "model_hint": model_hint or None,
            # Aggregate cost across the trace's observations (calculatedTotalCost
            # on the trace itself sums them). Langfuse calls this `totalCost`.
            "cost_usd": float(t.get("totalCost") or 0.0),
            "latency_ms": t.get("latency"),  # in ms; null if not measured
        })

    # Newest first; the public API usually returns this order but be explicit
    events.sort(key=lambda e: e["timestamp"] or "", reverse=True)
    return {
        "window_hours": hours,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total": len(events),
        "events": events[:limit],
    }


def _trace_input_preview(raw, max_chars: int = 200) -> str | None:
    """Pull a user query out of the trace's `input` blob. Trace input shape
    is inconsistent across our call sites:
      - chat route invocation: dict with `messages: [{role, content}, ...]`
      - direct LLM call: string or list of messages
      - structured-output call: dict with various shapes
    We grab the most-recent `user` message when we can identify one, else
    fall back to a string preview of the whole blob.
    """
    if raw is None:
        return None
    # Case 1: dict shaped like a chat-completions request
    if isinstance(raw, dict):
        msgs = raw.get("messages")
        if isinstance(msgs, list):
            user_msgs = [m for m in msgs if isinstance(m, dict) and m.get("role") == "user"]
            if user_msgs:
                content = user_msgs[-1].get("content")
                if isinstance(content, str):
                    return content[:max_chars]
                if isinstance(content, list):
                    # Anthropic-style content blocks; pull text parts
                    text = " ".join(
                        b.get("text", "") for b in content if isinstance(b, dict)
                    )
                    return text[:max_chars] or None
    # Case 2: list of messages
    if isinstance(raw, list):
        for m in reversed(raw):
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str):
                    return c[:max_chars]
    # Case 3: bare string
    if isinstance(raw, str):
        return raw[:max_chars]
    return None


# ── /admin/evaluations — eval-snapshot time series (Sprint 9.0.2) ───────

@router.get("/evaluations")
async def admin_evaluations(user: User = Depends(get_current_user)):
    """Surface the FinanceBench eval-results snapshots as a time series.

    The frontend dashboard's "Evaluations" tab renders pass rate, cost,
    RAGAS/DeepEval scores, and slice-level metrics over the campaign
    history. Each `tests/evaluation/eval_results/financebench_*.json`
    snapshot becomes one point on the time series.

    Files filtered out: `.pipeline.json`, `.ragas.json`, `.deepeval.json`,
    `.correctness.json`, `.review.json`, `.patronus.json` (per-sample
    drill-downs, not headline snapshots), and `_manifest.json`.

    Source-of-truth lives in committed files so this endpoint is
    deterministic and doesn't require Langfuse / Postgres / Qdrant.
    """
    _require_admin(user)

    root = Path("tests/evaluation/eval_results")
    if not root.exists():
        return {"snapshots": [], "total": 0}

    excludes = (".pipeline.json", ".ragas.json", ".deepeval.json", ".correctness.json",
                ".review.json", ".patronus.json", "_manifest.json")
    snapshots: list[dict] = []
    for fp in sorted(root.glob("financebench_*.json")):
        name = fp.name
        if any(name.endswith(ex) for ex in excludes):
            continue
        try:
            with fp.open() as f:
                data = json.load(f)
        except (ValueError, OSError) as e:
            logger.warning("Skipping unreadable eval snapshot %s: %s", name, e)
            continue
        snapshots.append(_eval_snapshot_to_record(name, fp, data))

    # Order by file mtime so the most recent eval is last (matches a
    # natural "over time" reading on a chart)
    snapshots.sort(key=lambda s: s["mtime"])
    return {"snapshots": snapshots, "total": len(snapshots)}


def _parse_or_dict(v) -> dict:
    """Older eval snapshots embed metric dicts as JSON strings; newer ones
    store them as objects. Normalize to a dict either way."""
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return {}
    return {}


def _eval_snapshot_to_record(filename: str, path, data: dict) -> dict:
    """Reduce an eval-results JSON to the headline metrics the frontend
    chart needs. Defensive about missing keys — old snapshots may have
    only some of the metric families."""
    correctness = _parse_or_dict(data.get("correctness"))
    ragas = _parse_or_dict(data.get("ragas"))
    deepeval = _parse_or_dict(data.get("deepeval"))
    diagnostics = _parse_or_dict(data.get("diagnostics"))

    # Strip the financebench_ prefix and the .json suffix for the display label
    label = filename.removeprefix("financebench_").removesuffix(".json")

    return {
        "filename": filename,
        "label": label,
        "mtime": os.path.getmtime(path),
        "num_samples": data.get("num_samples"),
        "pipeline_time_seconds": data.get("pipeline_time_seconds"),
        "correctness": {
            "pass_rate": correctness.get("pass_rate"),
            "n_pass": correctness.get("n_pass"),
            "n_samples": correctness.get("n_samples"),
        },
        "ragas": {
            "faithfulness": ragas.get("faithfulness"),
            "answer_relevancy": ragas.get("answer_relevancy"),
            "context_precision": ragas.get("context_precision"),
            "context_recall": ragas.get("context_recall"),
        },
        "deepeval": {
            "faithfulness": deepeval.get("faithfulness"),
            "contextual_precision": deepeval.get("contextual_precision"),
            "contextual_recall": deepeval.get("contextual_recall"),
        },
        "diagnostics": {
            "refusal_rate": diagnostics.get("refusal_rate"),
            "pass_when_answered": diagnostics.get("pass_when_answered"),
            "slice_summary": diagnostics.get("slice_summary"),
        },
    }
