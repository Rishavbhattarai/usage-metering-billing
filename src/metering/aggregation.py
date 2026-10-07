"""Aggregator: roll usage_events up into usage_hourly (and later daily).

The usage_hourly table already exists (migration 0001). This module is intentionally
left unimplemented.
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession


# YOUR TURN (see YOUR_TURN.md, push 2/3): implement the hourly aggregator.
# Hints: one SQL statement, INSERT ... SELECT date_trunc('hour', occurred_at) ... GROUP BY ...
# ON CONFLICT (customer_id, meter, hour) DO UPDATE. Buckets must be UTC hours (a CHECK
# constraint enforces it), and date_trunc on a timestamptz uses the session TimeZone.
# Decide recompute-vs-incremental and record it in docs/adr/0002-aggregation-strategy.md.
# Running it twice must give identical rows.
async def aggregate_hourly(session: AsyncSession, start: datetime, end: datetime) -> int:
    """Recompute usage_hourly for hours in [start, end). Returns the number of rows written."""
    raise NotImplementedError("YOUR TURN (push 2/3): hourly aggregator")
