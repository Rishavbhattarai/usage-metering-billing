"""Hourly aggregator: roll usage_events up into usage_hourly. Daily usage is the
`usage_daily` view over usage_hourly.

Strategy (ADR 0002): recompute whole cells. A cell is (customer_id, meter, UTC hour). Each
run finds the cells touched by events received since the last run and recomputes each one
from *all* of its raw events with one INSERT ... SELECT ... ON CONFLICT DO UPDATE. Running it
twice gives the same rows, and late events just make their old hour dirty again.

Watermark: events get received_at = their transaction's start time, so an ingest transaction
that started before the last run but committed after it would carry a received_at older than
the watermark. Each run therefore re-scans `overlap` seconds before the watermark.
Recomputing a clean cell is harmless, so overlap only costs a little extra work.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_UPSERT_CELLS = """
INSERT INTO usage_hourly (customer_id, meter, hour, quantity, updated_at)
SELECT e.customer_id, e.meter, d.hour, sum(e.quantity), now()
FROM dirty d
JOIN usage_events e
  ON e.customer_id = d.customer_id
 AND e.meter = d.meter
 AND e.occurred_at >= d.hour
 AND e.occurred_at < d.hour + interval '1 hour'
GROUP BY e.customer_id, e.meter, d.hour
ON CONFLICT (customer_id, meter, hour) DO UPDATE
   SET quantity = EXCLUDED.quantity, updated_at = EXCLUDED.updated_at
 WHERE usage_hourly.quantity IS DISTINCT FROM EXCLUDED.quantity
"""

# date_trunc on a timestamptz would use the session TimeZone; truncate in UTC explicitly.
_HOUR = "date_trunc('hour', occurred_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"


@dataclass(frozen=True, slots=True)
class AggregationResult:
    dirty_cells: int
    rows_changed: int
    watermark: datetime | None


async def aggregate_range(session: AsyncSession, start: datetime, end: datetime) -> int:
    """Recompute every cell whose events occurred in [start, end). Returns rows changed."""
    result = await session.execute(
        text(
            f"""
            WITH dirty AS (
              SELECT DISTINCT customer_id, meter, {_HOUR} AS hour
              FROM usage_events WHERE occurred_at >= :start AND occurred_at < :end
            )
            {_UPSERT_CELLS}
            """
        ),
        {"start": start, "end": end},
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


async def aggregate_dirty(
    session: AsyncSession, overlap: timedelta = timedelta(seconds=30), name: str = "hourly"
) -> AggregationResult:
    """Recompute the cells touched since the last run, then advance the watermark.

    Caller owns the transaction. Concurrent runs serialize on an advisory lock.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"aggregate:{name}"}
    )
    watermark = (
        await session.execute(
            text("SELECT watermark FROM aggregation_state WHERE name = :n"), {"n": name}
        )
    ).scalar_one_or_none()

    await session.execute(text("DROP TABLE IF EXISTS dirty"))
    if watermark is None:  # first run: everything is dirty
        await session.execute(
            text(
                "CREATE TEMP TABLE dirty ON COMMIT DROP AS "
                f"SELECT DISTINCT customer_id, meter, {_HOUR} AS hour FROM usage_events"
            )
        )
    else:
        await session.execute(
            text(
                "CREATE TEMP TABLE dirty ON COMMIT DROP AS "
                f"SELECT DISTINCT customer_id, meter, {_HOUR} AS hour FROM usage_events "
                "WHERE received_at > :since"
            ),
            {"since": watermark - overlap},
        )
    # Temp tables have no statistics until analyzed; without them the planner joins every
    # dirty cell against every event of its customer (measured: 205 s vs 2 s at 900k events).
    await session.execute(text("ANALYZE dirty"))
    dirty = int((await session.execute(text("SELECT count(*) FROM dirty"))).scalar_one())
    changed = 0
    if dirty:
        result = await session.execute(text(_UPSERT_CELLS))
        changed = int(result.rowcount)  # type: ignore[attr-defined]
    new_watermark = (
        await session.execute(
            text(
                "INSERT INTO aggregation_state (name, watermark) VALUES (:n, now()) "
                "ON CONFLICT (name) DO UPDATE SET watermark = now(), updated_at = now() "
                "RETURNING watermark"
            ),
            {"n": name},
        )
    ).scalar_one()
    await session.execute(text("DROP TABLE dirty"))
    return AggregationResult(dirty, changed, new_watermark)
