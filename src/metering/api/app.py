"""FastAPI app: ingestion, billing endpoints and the customer usage dashboard.

Run: uvicorn --factory metering.api.app:create_app --host 0.0.0.0 --port 8001
"""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from metering import __version__, queries
from metering.aggregation import aggregate_dirty
from metering.config import Settings
from metering.ingestion import ingest_events
from metering.invoicing import CloseError, load_invoice, rerate_invoice, rerate_period
from metering.jobs.handlers import CLOSE, RECONCILE, SYNC
from metering.models import UsageEvent
from metering.periods import parse_period, period_of
from metering.rating import format_cents
from metering.runtime import Runtime
from metering.schemas import BatchIn, IngestResult, UsageEventIn, UsageEventOut

HERE = Path(__file__).parent
PeriodQ = Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


def _rt(request: Request) -> Runtime:
    rt: Runtime = request.app.state.runtime
    return rt


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with _rt(request).sessions() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]


def _period(p: str) -> str:
    try:
        parse_period(p)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None
    return p


def create_app(settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    rt = runtime or Runtime.build(settings)
    settings = rt.settings

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await rt.aclose()

    app = FastAPI(title="Usage Metering & Billing", version=__version__, lifespan=lifespan)
    app.state.runtime = rt
    app.state.settings = settings
    app.state.engine = rt.engine
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["money"] = format_cents

    # -- health / ingestion ---------------
    @app.get("/healthz")
    async def healthz(session: Session) -> dict[str, str]:
        await session.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.post("/v1/events", response_model=IngestResult)
    async def ingest_one(event: UsageEventIn, session: Session) -> IngestResult:
        """Ingest one event. Safe to retry: a repeated event_id reports duplicates=1."""
        async with session.begin():
            return await ingest_events(session, [event])

    @app.post("/v1/events/batch", response_model=IngestResult)
    async def ingest_batch(batch: BatchIn, session: Session) -> IngestResult:
        """Ingest a batch atomically (all rows or none). Safe to retry the whole batch."""
        if len(batch.events) > settings.max_batch_size:
            raise HTTPException(
                413, f"batch has {len(batch.events)} events; max is {settings.max_batch_size}"
            )
        async with session.begin():
            return await ingest_events(session, batch.events)

    @app.get("/v1/events/{event_id}", response_model=UsageEventOut)
    async def get_event(event_id: str, session: Session) -> UsageEvent:
        event = await session.get(UsageEvent, event_id)
        if event is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")
        return event

    # -- usage ---------------
    @app.post("/v1/aggregate")
    async def aggregate_now(session: Session) -> dict[str, Any]:
        """Run the hourly aggregator once (it also runs continuously in its own service)."""
        async with session.begin():
            res = await aggregate_dirty(
                session, overlap=timedelta(seconds=settings.aggregate_overlap_seconds)
            )
        return {"dirty_cells": res.dirty_cells, "rows_changed": res.rows_changed}

    @app.get("/v1/customers")
    async def list_customers(session: Session) -> list[dict[str, Any]]:
        return await queries.customers(session)

    @app.get("/v1/customers/{customer_id}/usage")
    async def customer_usage(
        customer_id: str,
        session: Session,
        period: PeriodQ = None,
        granularity: Annotated[str, Query(pattern="^(daily|hourly)$")] = "daily",
    ) -> dict[str, Any]:
        return await queries.usage(
            session, customer_id, period or period_of(datetime.now(UTC)), granularity
        )

    # -- invoices ---------------
    @app.get("/v1/invoices")
    async def list_invoices(
        session: Session, customer_id: str | None = None, period: PeriodQ = None
    ) -> list[dict[str, Any]]:
        return await queries.invoices(session, customer_id, period)

    @app.get("/v1/invoices/{invoice_id}")
    async def get_invoice(invoice_id: str, session: Session) -> dict[str, Any]:
        inv = await load_invoice(session, invoice_id)
        if inv is None:
            raise HTTPException(404, "invoice not found")
        return {**inv.canonical(), "content_hash": inv.content_hash}

    @app.get("/v1/invoices/{invoice_id}/rerate")
    async def rerate_one(invoice_id: str, session: Session) -> dict[str, Any]:
        """Recompute this invoice from raw events and diff it against the stored one."""
        diff = await rerate_invoice(session, invoice_id)
        if diff is None:
            raise HTTPException(404, "invoice not found")
        return diff.as_dict()

    # -- periods / jobs ---------------
    @app.get("/v1/periods")
    async def list_periods(session: Session) -> list[dict[str, Any]]:
        return await queries.periods(session)

    @app.post("/v1/periods/{period}/close", status_code=202)
    async def close(period: str) -> dict[str, str]:
        """Enqueue the month-end close (then sync and reconcile). Idempotent per period."""
        job_id = await rt.queue().enqueue(
            CLOSE, {"period": _period(period)}, idempotency_key=f"{CLOSE}:{period}"
        )
        return {"job_id": job_id}

    @app.post("/v1/periods/{period}/sync", status_code=202)
    async def sync(period: str, force: bool = False) -> dict[str, str]:
        run = uuid.uuid4().hex
        job_id = await rt.queue().enqueue(
            SYNC, {"period": _period(period), "force": force, "run": run}
        )
        return {"job_id": job_id}

    @app.post("/v1/periods/{period}/reconcile", status_code=202)
    async def reconcile(period: str, repair: bool = False) -> dict[str, str]:
        job_id = await rt.queue().enqueue(RECONCILE, {"period": _period(period), "repair": repair})
        return {"job_id": job_id}

    @app.get("/v1/periods/{period}/reconciliation")
    async def reconciliation(period: str, session: Session) -> dict[str, Any]:
        report = await queries.latest_reconciliation(session, _period(period))
        if report is None:
            raise HTTPException(404, "no reconciliation run for this period")
        return report

    @app.get("/v1/periods/{period}/rerate")
    async def rerate(period: str, session: Session) -> dict[str, Any]:
        try:
            diffs = await rerate_period(session, _period(period))
        except CloseError as exc:
            raise HTTPException(409, str(exc)) from None
        changed = [d.as_dict() for d in diffs if not d.identical]
        return {
            "period": period,
            "invoices": len(diffs),
            "identical": len(diffs) - len(changed),
            "changed": changed[:100],
            "delta_cents": sum(d.delta_cents for d in diffs),
        }

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, Any]:
        job = await rt.queue().get(job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return {
            "id": job.id,
            "job_type": job.job_type,
            "status": job.status,
            "attempts": job.attempts,
            "result": job.result,
            "error": job.error,
        }

    # -- dashboard ---------------
    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/dashboard")

    @app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard(request: Request, session: Session) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "customers": await queries.customers(session),
                "periods": await queries.periods(session),
            },
        )

    @app.get(
        "/dashboard/customers/{customer_id}", response_class=HTMLResponse, include_in_schema=False
    )
    async def customer_page(
        request: Request, customer_id: str, session: Session, period: PeriodQ = None
    ) -> HTMLResponse:
        period = period or period_of(datetime.now(UTC))
        return templates.TemplateResponse(
            request,
            "customer.html",
            {
                "customer_id": customer_id,
                "period": period,
                "usage": await queries.usage(session, customer_id, period),
                "invoices": await queries.invoices(session, customer_id),
            },
        )

    @app.get(
        "/dashboard/customers/{customer_id}/usage",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def customer_usage_partial(
        request: Request, customer_id: str, session: Session, period: PeriodQ = None
    ) -> HTMLResponse:
        period = period or period_of(datetime.now(UTC))
        return templates.TemplateResponse(
            request, "_usage.html", {"usage": await queries.usage(session, customer_id, period)}
        )

    @app.get(
        "/dashboard/invoices/{invoice_id}", response_class=HTMLResponse, include_in_schema=False
    )
    async def invoice_page(request: Request, invoice_id: str, session: Session) -> HTMLResponse:
        inv = await load_invoice(session, invoice_id)
        if inv is None:
            raise HTTPException(404, "invoice not found")
        return templates.TemplateResponse(
            request, "invoice.html", {"inv": inv.canonical(), "hash": inv.content_hash}
        )

    return app
