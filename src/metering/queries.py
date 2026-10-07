"""Read models shared by the JSON API and the dashboard."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from metering.invoicing import charge
from metering.periods import period_end, period_start
from metering.pricing import plans_for_period


@dataclass(frozen=True, slots=True)
class MeterUsage:
    meter: str
    quantity: Decimal
    estimate_cents: int | None  # None when the meter has no price plan


async def customers(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            """
            SELECT customer_id, count(DISTINCT meter) AS meters, max(updated_at) AS updated_at,
                   (SELECT count(*) FROM invoices i WHERE i.customer_id = h.customer_id) AS invoices
            FROM usage_hourly h GROUP BY customer_id ORDER BY customer_id
            """
        )
    )
    return [dict(r._mapping) for r in rows]


async def usage(
    session: AsyncSession, customer_id: str, period: str, granularity: str = "daily"
) -> dict[str, Any]:
    start, end = period_start(period), period_end(period)
    if granularity == "hourly":
        q = (
            "SELECT meter, hour AS bucket, quantity FROM usage_hourly "
            "WHERE customer_id = :c AND hour >= :s AND hour < :e ORDER BY bucket, meter"
        )
        params: dict[str, Any] = {"c": customer_id, "s": start, "e": end}
    else:
        q = (
            "SELECT meter, day AS bucket, quantity FROM usage_daily "
            "WHERE customer_id = :c AND day >= :s AND day < :e ORDER BY bucket, meter"
        )
        params = {"c": customer_id, "s": start.date(), "e": end.date()}
    rows = (await session.execute(text(q), params)).all()
    totals: dict[str, Decimal] = {}
    for r in rows:
        totals[r.meter] = totals.get(r.meter, Decimal(0)) + r.quantity
    plans = await plans_for_period(session, period)
    meters = [
        MeterUsage(m, qty, charge(plans[m], qty) if m in plans else None)
        for m, qty in sorted(totals.items())
    ]
    updated = (
        await session.execute(
            text("SELECT max(updated_at) FROM usage_hourly WHERE customer_id = :c"),
            {"c": customer_id},
        )
    ).scalar_one()
    return {
        "customer_id": customer_id,
        "period": period,
        "granularity": granularity,
        "rows": [
            {"meter": r.meter, "bucket": _iso(r.bucket), "quantity": str(r.quantity)} for r in rows
        ],
        "meters": [
            {"meter": m.meter, "quantity": str(m.quantity), "estimate_cents": m.estimate_cents}
            for m in meters
        ],
        "estimate_cents": sum(m.estimate_cents or 0 for m in meters),
        "aggregated_through": updated.isoformat() if updated else None,
    }


async def invoices(
    session: AsyncSession, customer_id: str | None = None, period: str | None = None
) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            """
            SELECT i.id, i.customer_id, i.period, i.status, i.total_cents, i.content_hash,
                   i.created_at, s.status AS sf_status, s.synced_at AS sf_synced_at
            FROM invoices i
            LEFT JOIN sf_sync_log s ON s.entity = 'Invoice__c' AND s.external_id = i.id
            WHERE (CAST(:c AS text) IS NULL OR i.customer_id = :c)
              AND (CAST(:p AS text) IS NULL OR i.period = :p)
            ORDER BY i.period DESC, i.customer_id
            LIMIT 500
            """
        ),
        {"c": customer_id, "p": period},
    )
    return [dict(r._mapping) for r in rows]


async def periods(session: AsyncSession) -> list[dict[str, Any]]:
    rows = await session.execute(
        text(
            """
            SELECT b.period, b.closed_at, count(i.id) AS invoices,
                   coalesce(sum(i.total_cents), 0)::bigint AS total_cents,
                   (SELECT r.missing + r.mismatched FROM reconciliation_runs r
                    WHERE r.period = b.period ORDER BY r.id DESC LIMIT 1) AS drift,
                   (SELECT r.ran_at FROM reconciliation_runs r
                    WHERE r.period = b.period ORDER BY r.id DESC LIMIT 1) AS reconciled_at
            FROM billing_periods b LEFT JOIN invoices i ON i.period = b.period
            GROUP BY b.period, b.closed_at ORDER BY b.period DESC
            """
        )
    )
    return [dict(r._mapping) for r in rows]


async def latest_reconciliation(session: AsyncSession, period: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                "SELECT id, ran_at, details FROM reconciliation_runs "
                "WHERE period = :p ORDER BY id DESC LIMIT 1"
            ),
            {"p": period},
        )
    ).one_or_none()
    if row is None:
        return None
    return {"id": row.id, "ran_at": row.ran_at.isoformat(), **row.details}


def _iso(v: date | datetime) -> str:
    return v.isoformat()
