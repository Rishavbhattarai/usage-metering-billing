"""Aggregation, month-end close, late events, re-rating and replay against real Postgres."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from itertools import count
from pathlib import Path

import pytest
from sqlalchemy import text

from metering.aggregation import aggregate_dirty, aggregate_range
from metering.ingestion import ingest_events
from metering.invoicing import CloseError, close_period, load_invoice, rerate_period
from metering.pricing import upsert_plan
from metering.rating import FlatPricing, PricePlan
from metering.replay import replay_check
from metering.runtime import Runtime
from metering.schemas import UsageEventIn

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]
_ids = count()


async def send(rt: Runtime, *events: tuple[str, str, str, datetime]) -> None:
    """Ingest (customer, meter, quantity, occurred_at) events in one transaction."""
    batch = [
        UsageEventIn(
            event_id=f"t{next(_ids)}", customer_id=c, meter=m, quantity=D(q), occurred_at=at
        )
        for c, m, q, at in events
    ]
    async with rt.sessions() as session, session.begin():
        await ingest_events(session, batch)


async def close(rt: Runtime, period: str) -> object:
    async with rt.sessions() as session, session.begin():
        return await close_period(session, period, cutoff_lag=timedelta(0))


def aug(day: int, hour: int = 12, minute: int = 0, second: int = 0, us: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute, second, us, tzinfo=UTC)


def sep(day: int) -> datetime:
    return datetime(2026, 9, day, 12, tzinfo=UTC)


async def hourly(rt: Runtime) -> list[tuple[str, str, datetime, D]]:
    async with rt.sessions() as session:
        rows = await session.execute(
            text("SELECT customer_id, meter, hour, quantity FROM usage_hourly ORDER BY 1, 2, 3")
        )
        return [tuple(r) for r in rows]


# ---------------------------------------------------------------------------------- aggregation


async def test_aggregator_is_idempotent_and_splits_hours_exactly(rt: Runtime) -> None:
    await send(
        rt,
        ("c1", "api_calls", "1", aug(1, 10, 59, 59, 999_000)),
        ("c1", "api_calls", "2", aug(1, 11)),
        ("c1", "api_calls", "4", aug(1, 11, 30)),
    )
    async with rt.sessions() as session, session.begin():
        first = await aggregate_dirty(session, overlap=timedelta(0))
    rows = await hourly(rt)
    assert [(r[2], r[3]) for r in rows] == [(aug(1, 10), D(1)), (aug(1, 11), D(6))]
    assert first.dirty_cells == 2

    # Running again changes nothing, and a full-range recompute gives the same rows.
    async with rt.sessions() as session, session.begin():
        again = await aggregate_dirty(session, overlap=timedelta(0))
        await aggregate_range(session, aug(1, 0), aug(2, 0))
    assert again.rows_changed == 0
    assert await hourly(rt) == rows


async def test_duplicates_are_not_double_counted_and_late_events_reopen_their_hour(
    rt: Runtime,
) -> None:
    event = UsageEventIn(
        event_id="dup-1", customer_id="c1", meter="api_calls", quantity=D(5), occurred_at=aug(3)
    )
    for _ in range(3):
        async with rt.sessions() as session, session.begin():
            await ingest_events(session, [event])
    async with rt.sessions() as session, session.begin():
        await aggregate_dirty(session, overlap=timedelta(0))
    assert (await hourly(rt))[0][3] == D(5)

    await send(rt, ("c1", "api_calls", "7", aug(3, 12, 45)))  # late, same hour
    async with rt.sessions() as session, session.begin():
        res = await aggregate_dirty(session, overlap=timedelta(0))
    assert res.dirty_cells == 1
    assert (await hourly(rt))[0][3] == D(12)

    async with rt.sessions() as session:
        daily = (await session.execute(text("SELECT day, quantity FROM usage_daily"))).all()
    assert [tuple(r) for r in daily] == [(date(2026, 8, 3), D(12))]


# ---------------------------------------------------------------------------------- invoicing


async def test_close_period_hand_checked_and_idempotent(rt: Runtime) -> None:
    await send(
        rt,
        ("acme", "api_calls", "12345.5", aug(2)),  # $820.00 + 2345.5 x $0.05 = $937.28
        ("acme", "gb_stored", "100", aug(2)),  # $2.30 -> $50.00 minimum commit
        ("beta", "api_calls", "999", aug(31, 23, 59, 59)),  # $99.90
        ("beta", "api_calls", "1", sep(1)),  # September usage: not on the August invoice
    )
    first = await close(rt, "2026-08")
    async with rt.sessions() as session:
        acme = await load_invoice(session, "inv_2026-08_acme")
        beta = await load_invoice(session, "inv_2026-08_beta")
    assert acme is not None and beta is not None
    assert acme.total_cents == 937_28 + 50_00
    assert [ln.kind for ln in acme.lines] == ["usage", "usage", "usage", "usage", "minimum"]
    assert beta.total_cents == 99_90

    second = await close(rt, "2026-08")
    assert second.already_closed and second.invoices == first.invoices == 2  # type: ignore[attr-defined]
    async with rt.sessions() as session:
        n = await session.scalar(text("SELECT count(*) FROM invoices"))
    assert n == 2


async def test_periods_close_in_order_and_only_when_over(rt: Runtime) -> None:
    await close(rt, "2026-07")
    with pytest.raises(CloseError, match="in order"):
        await close(rt, "2026-09")
    future = (datetime.now(UTC) + timedelta(days=62)).strftime("%Y-%m")
    with pytest.raises(CloseError):
        await close(rt, future)


async def test_late_event_becomes_next_period_adjustment_at_marginal_price(rt: Runtime) -> None:
    await send(rt, ("acme", "api_calls", "900", aug(10)), ("acme", "gb_stored", "100", aug(10)))
    await close(rt, "2026-08")  # api: $90.00, storage: $50.00 minimum

    # Arrive after the August cutoff: 200 more August calls, 10 more August GB, plus Sept usage.
    await send(
        rt,
        ("acme", "api_calls", "200", aug(15)),
        ("acme", "gb_stored", "10", aug(15)),
        ("acme", "api_calls", "10", sep(5)),
    )
    await close(rt, "2026-09")
    async with rt.sessions() as session:
        inv = await load_invoice(session, "inv_2026-09_acme")
        august = await load_invoice(session, "inv_2026-08_acme")
    assert inv is not None and august is not None
    assert august.total_cents == 90_00 + 50_00  # closed invoice never changes
    adj = {ln.meter: ln for ln in inv.lines if ln.kind == "adjustment"}
    # 900 -> 1100 calls: 100 more at $0.10 and 100 at $0.08 = $18.00 (not 200 x $0.10).
    assert adj["api_calls"].amount_cents == 18_00
    assert adj["api_calls"].usage_period == "2026-08"
    # 100 -> 110 GB is still under the $50 minimum: $0.00 adjustment.
    assert adj["gb_stored"].amount_cents == 0
    assert inv.total_cents == 1_00 + 18_00


async def test_rerate_detects_a_price_change(rt: Runtime) -> None:
    await send(rt, ("acme", "gb_stored", "10000", aug(1)))  # $230.00
    await close(rt, "2026-08")
    async with rt.sessions() as session:
        assert all(d.identical for d in await rerate_period(session, "2026-08"))
    async with rt.sessions() as session, session.begin():
        await upsert_plan(session, PricePlan("gb_stored", FlatPricing(D("0.02"))), date(2026, 8, 1))
    async with rt.sessions() as session:
        (diff,) = await rerate_period(session, "2026-08")
    assert (diff.stored_total_cents, diff.recomputed_total_cents) == (230_00, 200_00)
    assert diff.delta_cents == -30_00 and not diff.identical


async def test_replaying_the_event_log_twice_gives_identical_invoices(
    rt: Runtime, scratch_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALEMBIC_CONFIG", str(ROOT / "alembic.ini"))
    for i in range(40):
        await send(rt, (f"c{i % 7}", "api_calls", str(100 + i * 37), aug(1 + i % 28)))
    await close(rt, "2026-08")
    await send(rt, ("c1", "api_calls", "500", aug(20)), ("c2", "gb_stored", "3", sep(2)))
    await close(rt, "2026-09")

    report = await replay_check(rt, scratch_database_url, runs=2)
    assert report["stored_invoices"] == 7 + 2  # August: 7 customers; Sept: c1 (late) + c2
    assert report["identical_across_runs"] and report["all_match_stored"]
    assert all(r["events_replayed"] == 42 for r in report["runs"])
