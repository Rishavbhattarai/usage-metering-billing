"""Usage event generator for load tests and demos.

Produces a realistic, *messy* stream: duplicate deliveries (same event_id re-sent, as an
at-least-once producer would) and late events (occurred_at in the previous month, delivered
now). Deterministic for a given --seed, so runs are comparable.

Examples:
  # POST 100k events (10% dupes, 2% late) to a running API in batches of 1000
  python loadtest/generate_events.py --count 100000 --url http://localhost:8000

  # Just write JSON Lines to a file (no server needed)
  python loadtest/generate_events.py --count 10000 --out events.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

METERS = {"api_calls": (1, 50), "gb_stored": (1, 5000)}  # meter -> (min, max) per event


def month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def generate(
    count: int, dup_rate: float, late_rate: float, customers: int, seed: int, now: datetime
) -> Iterator[dict[str, Any]]:
    """Yield `count` events in total; about dup_rate of them are re-sends of earlier events."""
    rng = random.Random(seed)
    current = month_start(now)
    previous = month_start(current - timedelta(days=1))
    sent: list[dict[str, Any]] = []
    next_id = 0
    for _ in range(count):
        if sent and rng.random() < dup_rate:
            yield dict(rng.choice(sent))
            continue
        meter = rng.choice(list(METERS))
        lo, hi = METERS[meter]
        if rng.random() < late_rate:  # late: belongs to last month, arriving now
            span = (current - previous).total_seconds()
            occurred = previous + timedelta(seconds=rng.uniform(0, span))
        else:
            occurred = current + timedelta(seconds=rng.uniform(0, (now - current).total_seconds()))
        # Quantities go out as strings so no float ever touches the money path.
        quantity = Decimal(rng.randint(lo * 1000, hi * 1000)).scaleb(-3)
        event = {
            "event_id": f"lt-{seed}-{next_id:09d}",
            "customer_id": f"cust_{rng.randrange(customers):05d}",
            "meter": meter,
            "quantity": str(quantity),
            "occurred_at": occurred.isoformat(),
        }
        next_id += 1
        sent.append(event)
        yield event


def batched(items: Iterator[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--count", type=int, default=10_000, help="total events to send")
    p.add_argument("--dup-rate", type=float, default=0.10, help="fraction re-sent (default 0.10)")
    p.add_argument("--late-rate", type=float, default=0.02, help="fraction from last month")
    p.add_argument("--customers", type=int, default=100)
    p.add_argument("--batch-size", type=int, default=1000)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--url", help="API base URL, e.g. http://localhost:8000")
    p.add_argument("--out", help="write JSON Lines here instead of POSTing")
    args = p.parse_args(argv)
    if not args.url and not args.out:
        p.error("one of --url or --out is required")

    now = datetime.now(UTC)
    events = generate(args.count, args.dup_rate, args.late_rate, args.customers, args.seed, now)

    if args.out:
        with open(args.out, "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
        print(f"wrote {args.count} events to {args.out}")
        return 0

    totals = {"received": 0, "accepted": 0, "duplicates": 0}
    started = time.perf_counter()
    with httpx.Client(base_url=args.url, timeout=60) as client:
        for batch in batched(events, args.batch_size):
            resp = client.post("/v1/events/batch", json={"events": batch})
            resp.raise_for_status()
            for k in totals:
                totals[k] += resp.json()[k]
    elapsed = time.perf_counter() - started
    print(
        json.dumps(
            {
                **totals,
                "seconds": round(elapsed, 3),
                "events_per_sec": round(totals["received"] / elapsed),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
