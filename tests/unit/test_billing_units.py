import random
import sys
import types
from datetime import UTC, date, datetime
from decimal import Decimal as D
from typing import Any

import pytest

from metering.invoicing import Invoice, Line, charge, diff_invoices, invoice_id
from metering.models import PricePlanRow
from metering.periods import next_period, period_end, period_of, period_start, prev_period
from metering.pricing import DEFAULT_PLANS, plan_from_row, plan_to_values
from metering.retry import retry_async
from metering.salesforce.sync import cents_to_currency, payload_hash


def test_period_helpers() -> None:
    assert next_period("2026-12") == "2027-01"
    assert prev_period("2026-01") == "2025-12"
    assert period_start("2026-02") == datetime(2026, 2, 1, tzinfo=UTC)
    assert period_end("2026-12") == datetime(2027, 1, 1, tzinfo=UTC)
    assert period_of(datetime(2026, 8, 31, 23, 59, tzinfo=UTC)) == "2026-08"
    with pytest.raises(ValueError, match="YYYY-MM"):
        period_start("2026-13")


@pytest.mark.parametrize("meter", sorted(DEFAULT_PLANS))
def test_price_plan_round_trips_through_db_values(meter: str) -> None:
    plan = DEFAULT_PLANS[meter]
    values = plan_to_values(plan, date(2020, 1, 1))
    row = PricePlanRow(**{k: v for k, v in values.items() if k != "effective_from"})
    assert plan_from_row(row) == plan


def test_charge_has_no_minimum_without_usage() -> None:
    storage = DEFAULT_PLANS["gb_stored"]
    assert charge(storage, D(0)) == 0
    assert charge(storage, D(1)) == 50_00


def _invoice(amount: int) -> Invoice:
    line = Line("usage", "api_calls", "2026-08", "api_calls: flat", D("1.5"), D("0.10"), amount)
    return Invoice(invoice_id("acme", "2026-08"), "acme", "2026-08", (line,))


def test_content_hash_is_stable_and_sensitive() -> None:
    a, b = _invoice(15), _invoice(15)
    assert a.content_hash == b.content_hash
    assert a.canonical_json() == b.canonical_json()
    assert b'"quantity":"1.500000"' in a.canonical_json()
    assert _invoice(16).content_hash != a.content_hash


def test_diff_invoices_reports_changed_lines() -> None:
    diff = diff_invoices(_invoice(15), _invoice(20))
    assert not diff.identical
    assert diff.delta_cents == 5
    assert diff.changed_lines[0]["stored_cents"] == 15
    assert diff_invoices(_invoice(15), _invoice(15)).identical


def test_sync_payload_helpers() -> None:
    assert cents_to_currency(123_456) == "1234.56"
    assert cents_to_currency(5) == "0.05"
    assert payload_hash({"a": 1, "b": 2}) == payload_hash({"b": 2, "a": 1})


async def test_retry_backs_off_then_succeeds() -> None:
    sleeps: list[float] = []
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError("blip")
        return "ok"

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    result = await retry_async(
        flaky,
        retry_on=(ConnectionError,),
        attempts=3,
        base=1,
        cap=10,
        sleep=fake_sleep,
        rng=random.Random(0),
    )
    assert result == "ok"
    assert len(sleeps) == 2
    assert 0 <= sleeps[0] <= 1 and 0 <= sleeps[1] <= 2  # full jitter, doubling ceiling


async def test_retry_gives_up_and_reraises() -> None:
    async def always() -> None:
        raise ConnectionError("down")

    async def no_sleep(_: float) -> None:
        return None

    with pytest.raises(ConnectionError):
        await retry_async(always, retry_on=(ConnectionError,), attempts=2, sleep=no_sleep)
    with pytest.raises(ValueError, match="bad input"):  # not retryable: raised immediately
        await retry_async(_raise_value_error, retry_on=(ConnectionError,), sleep=no_sleep)


async def _raise_value_error() -> None:
    raise ValueError("bad input")


def test_handlers_register_on_jobq_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inside a jobq worker the module registers its jobs on jobq's handler registry."""
    registered: dict[str, Any] = {}

    class Registry:
        def __contains__(self, name: object) -> bool:
            return name in registered

        def register(self, name: str, fn: Any) -> None:
            registered[name] = fn

    fake = types.ModuleType("jobq.handlers")
    fake.registry = Registry()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "jobq", types.ModuleType("jobq"))
    monkeypatch.setitem(sys.modules, "jobq.handlers", fake)

    from metering.jobs import handlers

    assert handlers._register_with_jobq()
    assert sorted(registered) == sorted(handlers.JOBS)
    assert handlers._register_with_jobq()  # idempotent on re-import


def test_previous_period_for_cron_schedules() -> None:
    from metering.jobs.handlers import resolve_period

    assert resolve_period("previous", datetime(2026, 1, 1, 2, tzinfo=UTC)) == "2025-12"
    assert resolve_period("2026-08") == "2026-08"
