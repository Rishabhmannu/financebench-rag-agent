"""Admin-only endpoints for operational visibility.

Sprint 8 8d: `/admin/costs` aggregates LLM spend from the self-hosted
Langfuse instance over a configurable time window, broken down by user,
model, and trace name. Querying Langfuse keeps the rag-agent itself
stateless on the cost-tracking side — the proxy is the source of truth,
this endpoint is just a thin reader on top.
"""
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status

from src.api.dependencies import get_current_user
from src.config.settings import settings
from src.models.auth import User

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
