"""Metrics for the latency and fault-tolerance criteria: GET /admin/metrics (JSON) and
the /dashboard page that charts it.

Everything is derived from what the pipeline already records -- job_events (every
claim, stage_done, retry, dead-letter), jobs, and the LLM trace stored with each
synthesis artifact -- so there is no extra write path and history is included.
"""

from __future__ import annotations

import asyncio
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import and_, func, select

from vidnotes_common.models import job_artifacts, job_events, jobs
from vidnotes_common.status import STAGE_ORDER, TERMINAL

from .routes_admin import require_admin
from .routes_debug import _workers
from .state import AppState, get_state

PAGE = Path(__file__).parent / "static" / "dashboard.html"
DONE = ("COMPLETED", "COMPLETED_WITH_WARNINGS")
LENGTH_BUCKETS = [(0, 300, "< 5 min"), (300, 600, "5–10 min"), (600, 900, "10–15 min"), (900, 1201, "15–20 min")]

page_router = APIRouter()
router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


@page_router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_page() -> HTMLResponse:
    return HTMLResponse(PAGE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


def summarize(values: list[float]) -> dict[str, Any]:
    """count / avg / p50 / p95 / max, rounded; empty -> count 0 and nulls."""
    v = sorted(x for x in values if x is not None)
    if not v:
        return {"count": 0, "avg": None, "p50": None, "p95": None, "max": None}

    def pct(p: float) -> float:
        return v[min(len(v) - 1, max(0, round(p * (len(v) - 1))))]

    return {"count": len(v), "avg": round(statistics.fmean(v), 1), "p50": round(pct(0.5), 1),
            "p95": round(pct(0.95), 1), "max": round(v[-1], 1)}


def _ts(dt: datetime) -> float:
    return dt.timestamp()


def compute(rows: list[Any], events: list[Any], syntheses: list[Any], *, since: datetime, hours: int,
            now: datetime) -> dict[str, Any]:
    """Pure function over query results (unit-tested without a database)."""
    # ---- throughput -------------------------------------------------------------------
    buckets: dict[str, dict[str, int]] = {}
    start = since.replace(minute=0, second=0, microsecond=0)
    for i in range(hours + 1):
        h = start + timedelta(hours=i)
        if h <= now:
            buckets[h.isoformat()] = {"submitted": 0, "completed": 0, "failed": 0}
    status_counts: Counter[str] = Counter()
    for j in rows:
        status_counts[j.status] += 1
        key = j.created_at.replace(minute=0, second=0, microsecond=0).isoformat()
        if key in buckets:
            buckets[key]["submitted"] += 1
            if j.status in DONE:
                buckets[key]["completed"] += 1
            elif j.status in ("FAILED", "DEAD_LETTERED"):
                buckets[key]["failed"] += 1

    # ---- latency ------------------------------------------------------------------------
    e2e: list[float] = []
    by_len: dict[str, list[float]] = defaultdict(list)
    slowest: list[dict[str, Any]] = []
    for j in rows:
        if j.status in DONE and j.updated_at and j.created_at:
            sec = _ts(j.updated_at) - _ts(j.created_at)
            e2e.append(sec)
            for lo, hi, label in LENGTH_BUCKETS:
                if j.duration_sec is not None and lo <= j.duration_sec < hi:
                    by_len[label].append(sec)
            slowest.append({"job_id": str(j.id), "title": j.video_title, "duration_sec": j.duration_sec,
                            "seconds": round(sec, 1)})
    slowest.sort(key=lambda x: -x["seconds"])

    per_job: dict[Any, list[Any]] = defaultdict(list)
    for e in events:
        per_job[e.job_id].append(e)

    stage_secs: dict[str, list[float]] = defaultdict(list)
    queue_wait: list[float] = []
    handoff: list[float] = []
    retries: Counter[str] = Counter()
    deferrals = 0
    recoveries: list[dict[str, Any]] = []
    cache_hits: Counter[str] = Counter()
    created = {j.id: j.created_at for j in rows}
    for job_id, evs in per_job.items():
        evs.sort(key=lambda e: e.id)
        first_claim = None
        last_done_at = None
        for e in evs:
            d = e.detail or {}
            ev = d.get("event")
            if ev == "stage_done":
                if isinstance(d.get("seconds"), int | float):
                    stage_secs[d.get("stage") or "?"].append(float(d["seconds"]))
                if d.get("cached_from"):
                    cache_hits[d.get("stage") or "?"] += 1
                last_done_at = e.at
            elif ev == "claimed":
                if first_claim is None:
                    first_claim = e.at
                if d.get("deferred"):
                    deferrals += 1
                if last_done_at is not None and not d.get("recovered_from"):
                    handoff.append(_ts(e.at) - _ts(last_done_at))
                    last_done_at = None
                if d.get("recovered_from"):
                    recoveries.append({
                        "job_id": str(job_id), "stage": d.get("stage"), "at": e.at.isoformat(),
                        "from_worker": d["recovered_from"], "to_worker": e.worker_id,
                        "recovery_sec": d.get("recovery_sec"),
                    })
            elif ev == "retry_scheduled":
                retries[((d.get("error") or {}).get("code")) or "UNKNOWN"] += 1
        if first_claim is not None and job_id in created:
            queue_wait.append(_ts(first_claim) - _ts(created[job_id]))

    failures: Counter[str] = Counter()
    for j in rows:
        if j.status in ("FAILED", "DEAD_LETTERED"):
            err = j.error or {}
            code = (err.get("last_error") or {}).get("code") if j.status == "DEAD_LETTERED" else err.get("code")
            failures[f"{j.status}:{code or 'UNKNOWN'}"] += 1

    # ---- LLM -----------------------------------------------------------------------------
    tps: list[float] = []
    call_secs: list[float] = []
    queued: list[float] = []
    calls = parse_errors = resumed = 0
    models: Counter[str] = Counter()
    for a in syntheses:
        data = a.data or {}
        if data.get("cached_from"):
            continue
        models[data.get("model") or "?"] += 1
        for c in (data.get("llm_trace") or {}).get("calls", []):
            if c.get("resumed_from_checkpoint"):
                resumed += 1
                continue
            calls += 1
            if c.get("parse_error"):
                parse_errors += 1
            if isinstance(c.get("seconds"), int | float):
                call_secs.append(float(c["seconds"]))
            if isinstance(c.get("queued_sec"), int | float):
                queued.append(float(c["queued_sec"]))
            st = c.get("provider_stats") or {}
            if isinstance(st.get("tokens_per_sec"), int | float):
                tps.append(float(st["tokens_per_sec"]))

    order = [str(s) for s in STAGE_ORDER]
    return {
        "window": {"hours": hours, "since": since.isoformat(), "now": now.isoformat()},
        "jobs": {
            "total": len(rows),
            "by_status": dict(status_counts),
            "per_hour": [{"hour": k, **v} for k, v in buckets.items()],
            "in_flight": sum(1 for j in rows if j.status not in {str(s) for s in TERMINAL}),
        },
        "latency": {
            "end_to_end_sec": summarize(e2e),
            "by_video_length": {label: summarize(by_len.get(label, [])) for _, _, label in LENGTH_BUCKETS},
            "queue_wait_sec": summarize(queue_wait),
            "handoff_wait_sec": summarize(handoff),
            "stage_sec": {st: summarize(stage_secs.get(st, [])) for st in order},
            "slowest": slowest[:5],
        },
        "reliability": {
            "retries_by_code": dict(retries.most_common()),
            "retries_total": sum(retries.values()),
            "lock_deferrals": deferrals,
            "dead_lettered": status_counts.get("DEAD_LETTERED", 0),
            "failed": status_counts.get("FAILED", 0),
            "failures_by_code": dict(failures.most_common()),
            "llm_429s": retries.get("RATE_LIMITED", 0),
        },
        "recovery": {
            "events": sorted(recoveries, key=lambda r: r["at"], reverse=True)[:20],
            "recovery_sec": summarize([r["recovery_sec"] for r in recoveries if r["recovery_sec"] is not None]),
            "count": len(recoveries),
        },
        "llm": {
            "models": dict(models),
            "calls": calls,
            "parse_errors": parse_errors,
            "resumed_from_checkpoint": resumed,
            "tokens_per_sec": summarize(tps),
            "call_sec": summarize(call_secs),
            "queued_sec": summarize(queued),
        },
        "cache": {"hits_by_stage": dict(cache_hits), "hits": sum(cache_hits.values())},
    }


@router.get("/metrics")
async def metrics(hours: int = Query(24, ge=1, le=24 * 30), st: AppState = Depends(get_state)):
    now = datetime.now(UTC)
    since = now - timedelta(hours=hours)
    async with st.engine.connect() as conn:
        rows = (await conn.execute(select(jobs).where(jobs.c.created_at >= since))).all()
        ids = [r.id for r in rows]
        events = (await conn.execute(
            select(job_events.c.id, job_events.c.job_id, job_events.c.at, job_events.c.worker_id, job_events.c.detail)
            .where(job_events.c.job_id.in_(ids)).order_by(job_events.c.id)
        )).all() if ids else []
        syn = (await conn.execute(
            select(job_artifacts.c.job_id, job_artifacts.c.data)
            .where(and_(job_artifacts.c.stage == "synthesis", job_artifacts.c.job_id.in_(ids)))
        )).all() if ids else []
        all_time = (await conn.execute(select(func.count()).select_from(jobs))).scalar_one()
    out = compute(rows, events, syn, since=since, hours=hours, now=now)
    out["jobs"]["all_time"] = all_time

    async def live():
        q = await st.publisher.queue_depths()
        held = await st.redis.zcard("sem:llm:holders")
        waiting = await st.redis.zcard("sem:llm:queue")
        return {"queues": q, "llm_gate": {"limit": st.settings.llm_concurrency, "in_flight": int(held),
                                          "waiting": int(waiting)}}

    try:
        out["live"] = await asyncio.wait_for(live(), 3)
        out["live"]["workers"] = [
            {k: w.get(k) for k in ("worker_id", "state", "stage", "job_id", "handled", "failed", "last_seen")}
            for w in await asyncio.wait_for(_workers(st), 3)
        ]
    except Exception as e:  # noqa: BLE001 - metrics still useful without the live part
        out["live"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    return out
