"""Billing jobs. They run on Project 1's jobq (load this module in a jobq worker with
JOBQ_HANDLER_MODULES=metering.jobs.handlers) or in-process via InProcessJobQueue.

jobq delivers at least once, so every handler is idempotent:
  * billing.close_period: a closed period returns its stored result; UNIQUE(customer, period)
  * billing.sync_salesforce: upserts by External ID
  * billing.reconcile: read-only apart from appending a report row
  * billing.aggregate: recomputes whole hour cells
Chained jobs use idempotency keys, so a re-delivered close doesn't enqueue a second sync.
"""

import importlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from metering.aggregation import aggregate_dirty
from metering.invoicing import close_period
from metering.jobs.queue import JobQueue, Payload
from metering.periods import period_of, prev_period
from metering.runtime import Runtime, get_runtime
from metering.salesforce.sync import reconcile_period, sync_period

CLOSE = "billing.close_period"
SYNC = "billing.sync_salesforce"
RECONCILE = "billing.reconcile"
AGGREGATE = "billing.aggregate"


def resolve_period(value: str, now: datetime | None = None) -> str:
    """'previous' means the month before `now` (for a jobq cron schedule on the 1st)."""
    if value == "previous":
        return prev_period(period_of(now or datetime.now(UTC)))
    return value


async def close_period_job(payload: Payload, rt: Runtime, queue: JobQueue) -> dict[str, Any]:
    period = resolve_period(str(payload["period"]))
    async with rt.sessions() as session, session.begin():
        result = await close_period(
            session, period, cutoff_lag=timedelta(seconds=rt.settings.close_cutoff_lag_seconds)
        )
    out: dict[str, Any] = {
        "period": result.period,
        "closed_at": result.closed_at.isoformat(),
        "already_closed": result.already_closed,
        "invoices": result.invoices,
        "total_cents": result.total_cents,
        "adjustment_lines": result.adjustment_lines,
    }
    if payload.get("sync", True):
        out["sync_job_id"] = await queue.enqueue(
            SYNC, {"period": period, "run": "close"}, idempotency_key=f"{SYNC}:{period}:close"
        )
    return out


async def sync_job(payload: Payload, rt: Runtime, queue: JobQueue) -> dict[str, Any]:
    period = str(payload["period"])
    result = await sync_period(
        rt.sessions,
        rt.salesforce(),
        period,
        force=bool(payload.get("force", False)),
        attempts=rt.settings.sync_attempts,
        backoff_base=rt.settings.sync_backoff_base,
    )
    out = result.as_dict()
    if payload.get("reconcile", True):
        # Keyed by the sync run, so a re-delivered sync job doesn't reconcile twice but a
        # new sync run (e.g. after a repair) gets its own reconcile.
        run = payload.get("run", "close")
        out["reconcile_job_id"] = await queue.enqueue(
            RECONCILE, {"period": period}, idempotency_key=f"{RECONCILE}:{period}:{run}"
        )
    return out


async def reconcile_job(payload: Payload, rt: Runtime, queue: JobQueue) -> dict[str, Any]:
    report = await reconcile_period(
        rt.sessions, rt.salesforce(), str(payload["period"]), repair=bool(payload.get("repair"))
    )
    return report.as_dict()


async def aggregate_job(payload: Payload, rt: Runtime, queue: JobQueue) -> dict[str, Any]:
    async with rt.sessions() as session, session.begin():
        res = await aggregate_dirty(
            session, overlap=timedelta(seconds=rt.settings.aggregate_overlap_seconds)
        )
    return {"dirty_cells": res.dirty_cells, "rows_changed": res.rows_changed}


JobFn = Callable[[Payload, Runtime, JobQueue], Awaitable[dict[str, Any]]]
JOBS: dict[str, JobFn] = {
    CLOSE: close_period_job,
    SYNC: sync_job,
    RECONCILE: reconcile_job,
    AGGREGATE: aggregate_job,
}


def bind(
    fn: JobFn, rt_factory: Callable[[], Runtime], queue_factory: Callable[[], JobQueue]
) -> Callable[[Payload], Awaitable[dict[str, Any]]]:
    async def handler(payload: Payload) -> dict[str, Any]:
        return await fn(payload, rt_factory(), queue_factory())

    handler.__name__ = fn.__name__
    return handler


def _register_with_jobq() -> bool:
    """If running inside a jobq worker, register the billing handlers on its registry."""
    try:
        registry = importlib.import_module("jobq.handlers").registry
    except ImportError:
        return False
    for name, fn in JOBS.items():
        if name not in registry:
            registry.register(name, bind(fn, get_runtime, lambda: get_runtime().queue()))
    return True


REGISTERED_WITH_JOBQ = _register_with_jobq()
