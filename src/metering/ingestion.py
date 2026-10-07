"""Idempotent ingestion into the append-only usage_events table."""

from collections.abc import Sequence

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from metering.models import UsageEvent
from metering.schemas import IngestResult, UsageEventIn


async def ingest_events(session: AsyncSession, events: Sequence[UsageEventIn]) -> IngestResult:
    """Insert events, silently skipping any event_id that already exists.

    Dedupe is done by the database (PRIMARY KEY + ON CONFLICT DO NOTHING), so it holds
    across concurrent requests and API replicas, not just within this process.
    First write wins: a re-sent event_id with a different payload is still a duplicate.

    Caller owns the transaction.
    """
    received = len(events)
    # Drop repeats inside this request (first occurrence wins), then sort by event_id so
    # concurrent overlapping batches take row locks in the same order and cannot deadlock.
    unique: dict[str, UsageEventIn] = {}
    for e in events:
        unique.setdefault(e.event_id, e)
    rows = [e.model_dump() for _, e in sorted(unique.items())]
    if not rows:
        return IngestResult(received=0, accepted=0, duplicates=0)

    stmt = (
        pg_insert(UsageEvent)
        .on_conflict_do_nothing(index_elements=[UsageEvent.event_id])
        .returning(UsageEvent.event_id)
    )
    result = await session.execute(stmt, rows)
    accepted = len(result.scalars().all())
    return IngestResult(received=received, accepted=accepted, duplicates=received - accepted)
