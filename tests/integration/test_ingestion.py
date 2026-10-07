import asyncio
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration

BASE_TIME = datetime(2026, 9, 1, tzinfo=UTC)


def make_event(i: int, rng: random.Random) -> dict[str, Any]:
    return {
        "event_id": f"evt_{i:07d}",
        "customer_id": f"cust_{rng.randrange(50):03d}",
        "meter": rng.choice(["api_calls", "gb_stored"]),
        "quantity": str(Decimal(rng.randrange(1, 10_000_000)).scaleb(-3)),
        "occurred_at": (BASE_TIME + timedelta(seconds=rng.randrange(30 * 86400))).isoformat(),
    }


async def count_rows(app: FastAPI) -> int:
    async with app.state.engine.connect() as conn:
        return int((await conn.execute(text("SELECT count(*) FROM usage_events"))).scalar_one())


async def post_batches(
    client: httpx.AsyncClient, events: list[dict[str, Any]], size: int
) -> tuple[int, int, int]:
    received = accepted = duplicates = 0
    for start in range(0, len(events), size):
        resp = await client.post("/v1/events/batch", json={"events": events[start : start + size]})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["received"] == body["accepted"] + body["duplicates"]
        received += body["received"]
        accepted += body["accepted"]
        duplicates += body["duplicates"]
    return received, accepted, duplicates


async def test_10k_events_with_10pct_duplicates_gives_exactly_9k_rows(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """Week 1 milestone: 10k events with 10% duplicates -> exactly 9k unique rows."""
    rng = random.Random(42)
    unique = [make_event(i, rng) for i in range(9_000)]
    # 1,000 re-sends of already-generated events, shuffled in, so duplicates land both
    # inside the same batch and across batches.
    events = unique + [dict(e) for e in rng.choices(unique, k=1_000)]
    rng.shuffle(events)
    assert len(events) == 10_000

    received, accepted, duplicates = await post_batches(client, events, size=500)

    assert (received, accepted, duplicates) == (10_000, 9_000, 1_000)
    assert await count_rows(app) == 9_000

    # Replaying the whole stream (e.g. a producer retrying everything) changes nothing.
    received, accepted, duplicates = await post_batches(client, events, size=1_000)
    assert (received, accepted, duplicates) == (10_000, 0, 10_000)
    assert await count_rows(app) == 9_000


async def test_concurrent_overlapping_batches_store_each_event_once(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    rng = random.Random(7)
    events = [make_event(i, rng) for i in range(1_000)]

    async def send(seed: int) -> int:
        batch = list(events)
        random.Random(seed).shuffle(batch)  # different orders: must not deadlock
        resp = await client.post("/v1/events/batch", json={"events": batch})
        assert resp.status_code == 200, resp.text
        return int(resp.json()["accepted"])

    accepted = await asyncio.gather(*(send(s) for s in range(8)))
    assert sum(accepted) == 1_000
    assert await count_rows(app) == 1_000


async def test_single_event_endpoint_is_idempotent_and_exact(client: httpx.AsyncClient) -> None:
    event = {
        "event_id": "evt_single",
        "customer_id": "cust_1",
        "meter": "gb_stored",
        "quantity": "12345678901234.000001",
        "occurred_at": "2026-09-30T23:59:59.999+00:00",
    }
    first = await client.post("/v1/events", json=event)
    assert first.json() == {"received": 1, "accepted": 1, "duplicates": 0}
    second = await client.post("/v1/events", json=event)
    assert second.json() == {"received": 1, "accepted": 0, "duplicates": 1}

    stored = (await client.get("/v1/events/evt_single")).json()
    assert Decimal(stored["quantity"]) == Decimal("12345678901234.000001")
    assert datetime.fromisoformat(stored["occurred_at"]) == datetime.fromisoformat(
        event["occurred_at"]
    )


async def test_first_write_wins_for_reused_event_id(client: httpx.AsyncClient) -> None:
    base = {
        "event_id": "evt_reused",
        "customer_id": "cust_1",
        "meter": "api_calls",
        "occurred_at": "2026-09-01T00:00:00Z",
    }
    await client.post("/v1/events", json={**base, "quantity": "5"})
    resp = await client.post("/v1/events", json={**base, "quantity": "500"})
    assert resp.json()["duplicates"] == 1
    assert Decimal((await client.get("/v1/events/evt_reused")).json()["quantity"]) == 5


async def test_usage_events_is_append_only(app: FastAPI, client: httpx.AsyncClient) -> None:
    await client.post("/v1/events", json=make_event(1, random.Random(1)))
    for sql in ("UPDATE usage_events SET quantity = 0", "DELETE FROM usage_events"):
        with pytest.raises(DBAPIError, match="append-only"):
            async with app.state.engine.begin() as conn:
                await conn.execute(text(sql))
    assert await count_rows(app) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quantity", "-1"),
        ("quantity", "0.0000001"),  # more than 6 decimal places
        ("quantity", "NaN"),
        ("occurred_at", "2026-09-01T00:00:00"),  # naive timestamp
        ("event_id", ""),
        ("event_id", "has space"),
    ],
)
async def test_invalid_events_are_rejected(
    app: FastAPI, client: httpx.AsyncClient, field: str, value: str
) -> None:
    event = make_event(1, random.Random(1)) | {field: value}
    resp = await client.post("/v1/events/batch", json={"events": [event]})
    assert resp.status_code == 422
    assert await count_rows(app) == 0


async def test_batch_over_limit_is_rejected(client: httpx.AsyncClient) -> None:
    rng = random.Random(3)
    resp = await client.post(
        "/v1/events/batch", json={"events": [make_event(i, rng) for i in range(1_001)]}
    )
    assert resp.status_code == 413


async def test_unknown_event_is_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/events/nope")).status_code == 404


async def test_healthz(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"status": "ok"}
