"""Ingestion load test: throughput, request latency and event-to-aggregate lag.

Sends a generated stream (with duplicates) through POST /v1/events/batch with N requests in
flight. While it runs, a probe sends one event every --probe-interval seconds for a reserved
customer and polls the hourly rollup until that event is counted; the time between sending a
probe and seeing it aggregated is the event-to-aggregate lag (it includes the aggregator's
polling interval).

  python loadtest/bench.py --url http://localhost:8001 --count 200000 --concurrency 8
"""

import argparse
import asyncio
import json
import statistics
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from generate_events import batched, generate


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1))))
    return ordered[k]


async def send_all(
    client: httpx.AsyncClient, batches: list[list[dict[str, Any]]], concurrency: int
) -> tuple[dict[str, int], list[float]]:
    totals = {"received": 0, "accepted": 0, "duplicates": 0}
    latencies: list[float] = []
    queue: asyncio.Queue[list[dict[str, Any]]] = asyncio.Queue()
    for b in batches:
        queue.put_nowait(b)

    async def worker() -> None:
        while not queue.empty():
            batch = queue.get_nowait()
            t0 = time.perf_counter()
            resp = await client.post("/v1/events/batch", json={"events": batch})
            latencies.append(time.perf_counter() - t0)
            resp.raise_for_status()
            for k, v in resp.json().items():
                totals[k] += v

    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return totals, latencies


async def probe_lag(client: httpx.AsyncClient, stop: asyncio.Event, interval: float) -> list[float]:
    """Send probe events; return per-probe lag until each is visible in usage_hourly."""
    customer = f"lagprobe-{uuid.uuid4().hex[:8]}"
    lags: list[float] = []
    sent = 0
    while not stop.is_set():
        sent += 1
        now = datetime.now(UTC)
        await client.post(
            "/v1/events",
            json={
                "event_id": f"{customer}-{sent}",
                "customer_id": customer,
                "meter": "api_calls",
                "quantity": "1",
                "occurred_at": now.isoformat(),
            },
        )
        t_sent = time.perf_counter()
        period = now.strftime("%Y-%m")
        while True:
            usage = (
                await client.get(f"/v1/customers/{customer}/usage", params={"period": period})
            ).json()
            seen = sum(int(float(m["quantity"])) for m in usage["meters"])
            if seen >= sent:
                lags.append(time.perf_counter() - t_sent)
                break
            if time.perf_counter() - t_sent > 60:
                raise TimeoutError("probe not aggregated within 60s; is the aggregator running?")
            await asyncio.sleep(0.05)
        await asyncio.sleep(interval)
    return lags


async def run(args: argparse.Namespace) -> dict[str, Any]:
    start = datetime.strptime(args.period, "%Y-%m").replace(tzinfo=UTC) if args.period else None
    events = generate(
        args.count,
        args.dup_rate,
        args.late_rate,
        args.customers,
        args.seed,
        datetime.now(UTC),
        period_start=start,
        id_prefix=args.id_prefix,
    )
    batches = list(batched(events, args.batch_size))
    limits = httpx.Limits(max_connections=args.concurrency + 4)
    async with httpx.AsyncClient(base_url=args.url, timeout=120, limits=limits) as client:
        stop = asyncio.Event()
        probe = (
            asyncio.create_task(probe_lag(client, stop, args.probe_interval)) if args.lag else None
        )
        t0 = time.perf_counter()
        totals, latencies = await send_all(client, batches, args.concurrency)
        elapsed = time.perf_counter() - t0
        if probe and args.probe_seconds > elapsed:  # keep probing (e.g. lag with no load)
            await asyncio.sleep(args.probe_seconds - elapsed)
        stop.set()
        lags = await probe if probe else []
    return {
        **totals,
        "batches": len(batches),
        "batch_size": args.batch_size,
        "concurrency": args.concurrency,
        "seconds": round(elapsed, 2),
        "events_per_sec": round(totals["received"] / elapsed) if elapsed else 0,
        "batch_latency_ms": {
            "p50": round(pct(latencies, 50) * 1000, 1),
            "p95": round(pct(latencies, 95) * 1000, 1),
            "p99": round(pct(latencies, 99) * 1000, 1),
            "max": round(max(latencies) * 1000, 1),
        }
        if latencies
        else None,
        "lag_ms": {
            "probes": len(lags),
            "p50": round(statistics.median(lags) * 1000) if lags else None,
            "p95": round(pct(lags, 95) * 1000) if lags else None,
            "max": round(max(lags) * 1000) if lags else None,
        },
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--url", default="http://localhost:8001")
    p.add_argument("--count", type=int, default=100_000)
    p.add_argument("--dup-rate", type=float, default=0.10)
    p.add_argument("--late-rate", type=float, default=0.0)
    p.add_argument("--customers", type=int, default=100)
    p.add_argument("--batch-size", type=int, default=1000)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--period", help="YYYY-MM to spread events over (default: this month so far)")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--id-prefix", default="bench")
    p.add_argument("--no-lag", dest="lag", action="store_false")
    p.add_argument("--probe-interval", type=float, default=0.5)
    p.add_argument("--probe-seconds", type=float, default=0, help="probe for at least this long")
    p.add_argument("--out", help="also write the JSON result here")
    args = p.parse_args()
    result = asyncio.run(run(args))
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
