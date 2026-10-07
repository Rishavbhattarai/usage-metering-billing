"""End-to-end demo against the running docker compose stack.

  1. ingest --events usage events for --period (10% duplicates) through the API
  2. wait for the aggregator; check the hourly rollup equals the raw totals
  3. (--fail-sync) make the fake Salesforce fail the next sync calls
  4. close --period as a jobq job; it chains a Salesforce sync and a reconciliation.
     A failed sync attempt is retried by jobq with backoff.
  5. send late events for --period plus usage for the next month, close the next month:
     the late usage shows up as adjustment lines
  6. show matching totals: ledger, invoice, Salesforce (fake), with zero drift
  7. (--dlq) tamper a record in Salesforce, keep the sync failing until jobq dead-letters
     it, then replay it from the DLQ and reconcile back to zero drift
  8. re-rate both periods from raw events (identical), and (--replay) replay the whole
     event log twice into a scratch database and compare invoice hashes

Requires the stack started with a short close cutoff, e.g.
  CLOSE_CUTOFF_LAG_SECONDS=3 docker compose up -d --build --wait
  python loadtest/demo.py --url http://localhost:8001 --events 1000000 --fail-sync --replay
"""

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from bench import send_all
from generate_events import batched, generate

SCRATCH_URL = "postgresql+asyncpg://metering:metering@postgres:5432/metering_replay"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def compose(args: argparse.Namespace, *cmd: str) -> str:
    full = [*shlex.split(args.compose), "exec", "-T", *cmd]
    return subprocess.run(full, check=True, capture_output=True, text=True).stdout


def next_month(period: str) -> str:
    y, m = map(int, period.split("-"))
    return f"{y + m // 12}-{m % 12 + 1:02d}"


async def wait_job(client: httpx.AsyncClient, job_id: str, limit: float = 600) -> dict[str, Any]:
    deadline = time.monotonic() + limit
    while True:
        job: dict[str, Any] = (await client.get(f"/v1/jobs/{job_id}")).json()
        if job["status"] in ("succeeded", "dead"):
            return job
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} still {job['status']}")
        await asyncio.sleep(0.5)


async def ingest(
    client: httpx.AsyncClient, events: Any, args: argparse.Namespace
) -> dict[str, Any]:
    t0 = time.perf_counter()
    totals, _ = await send_all(client, list(batched(events, args.batch_size)), args.concurrency)
    secs = time.perf_counter() - t0
    return {**totals, "seconds": round(secs, 2), "events_per_sec": round(totals["received"] / secs)}


async def close_chain(client: httpx.AsyncClient, period: str) -> dict[str, Any]:
    """Close a period via jobq and follow close -> sync -> reconcile."""
    t0 = time.perf_counter()
    close_id = (await client.post(f"/v1/periods/{period}/close")).json()["job_id"]
    close = await wait_job(client, close_id)
    assert close["status"] == "succeeded", close
    sync = await wait_job(client, close["result"]["sync_job_id"])
    assert sync["status"] == "succeeded", sync
    recon = await wait_job(client, sync["result"]["reconcile_job_id"])
    assert recon["status"] == "succeeded", recon
    return {
        "close": close["result"],
        "sync_attempts": sync["attempts"],
        "sync_sent": sync["result"]["sent"],
        "reconciliation": {
            k: recon["result"][k]
            for k in ("checked", "drift", "ledger_total_cents", "salesforce_total_cents")
        },
        "seconds": round(time.perf_counter() - t0, 2),
    }


async def dlq_round_trip(args: argparse.Namespace, period: str) -> dict[str, Any]:
    """Drift in Salesforce + a sync that keeps failing -> jobq dead-letters it -> fix the
    outage -> replay from the DLQ -> the chained reconcile reports zero drift."""
    async with (
        httpx.AsyncClient(base_url=args.url, timeout=60) as api,
        httpx.AsyncClient(base_url=args.jobq_url, timeout=60) as jobq,
    ):
        inv = (await api.get("/v1/invoices", params={"period": period})).json()[0]["id"]
        compose(
            args,
            "billing-worker",
            "metering",
            "fake-sf",
            "tamper",
            "Invoice__c",
            "Invoice_Ext_Id__c",
            inv,
            "Total__c",
            "0.01",
        )
        rid = (await api.post(f"/v1/periods/{period}/reconcile", params={"repair": True})).json()
        drift_before = (await wait_job(api, rid["job_id"]))["result"]["drift"]
        log(f"tampered {inv} in Salesforce: reconcile drift={drift_before}")

        compose(args, "billing-worker", "metering", "fake-sf", "fail-next", "1000")
        sync_id = (await api.post(f"/v1/periods/{period}/sync")).json()["job_id"]
        dead = await wait_job(api, sync_id, limit=180)
        dlq = (await jobq.get("/dlq", params={"type": "billing.sync_salesforce"})).json()
        in_dlq = any(j["id"] == sync_id for j in dlq["items"])
        log(
            f"sync during outage: status={dead['status']} after {dead['attempts']} attempts, "
            f"in DLQ={in_dlq}"
        )

        compose(args, "billing-worker", "metering", "fake-sf", "clear-failures")
        (await jobq.post(f"/dlq/{sync_id}/replay", json={"extra_attempts": 3})).raise_for_status()
        replayed = await wait_job(api, sync_id)
        recon = await wait_job(api, replayed["result"]["reconcile_job_id"])
        log(f"replayed from DLQ: {replayed['status']}; reconcile drift={recon['result']['drift']}")
        return {
            "drift_after_tamper": drift_before,
            "dead_status": dead["status"],
            "attempts_before_dlq": dead["attempts"],
            "in_dlq": in_dlq,
            "replayed_status": replayed["status"],
            "drift_after_replay": recon["result"]["drift"],
        }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {"events": args.events, "period": args.period}
    p1, p2 = args.period, next_month(args.period)
    start1 = datetime.strptime(p1, "%Y-%m").replace(tzinfo=UTC)
    start2 = datetime.strptime(p2, "%Y-%m").replace(tzinfo=UTC)
    limits = httpx.Limits(max_connections=args.concurrency + 4)
    async with httpx.AsyncClient(base_url=args.url, timeout=300, limits=limits) as client:
        log(f"ingesting {args.events:,} events for {p1} (10% duplicates)")
        out["ingest"] = await ingest(
            client,
            generate(
                args.events,
                0.10,
                0.0,
                args.customers,
                args.seed,
                datetime.now(UTC),
                period_start=start1,
                id_prefix="demo",
            ),
            args,
        )
        log(f"ingest: {out['ingest']}")

        t0 = time.perf_counter()
        agg = (await client.post("/v1/aggregate")).json()
        log(f"aggregator caught up: {agg} in {time.perf_counter() - t0:.2f}s")
        out["aggregate_check"] = (
            compose(
                args,
                "postgres",
                "psql",
                "-U",
                "metering",
                "-d",
                "metering",
                "-Atc",
                "SELECT (SELECT sum(quantity) FROM usage_events)"
                " = (SELECT sum(quantity) FROM usage_hourly)",
            ).strip()
            == "t"
        )
        log(f"hourly rollup total == raw event total: {out['aggregate_check']}")

        if args.fail_sync:
            compose(
                args, "billing-worker", "metering", "fake-sf", "fail-next", str(args.fail_calls)
            )
            log(f"fake Salesforce will fail the next {args.fail_calls} API calls")

        await asyncio.sleep(args.cutoff_lag + 1)  # let the ingested events pass the close cutoff
        log(f"closing {p1} via jobq (close -> sync -> reconcile)")
        out["close_1"] = await close_chain(client, p1)
        log(f"{p1}: {out['close_1']}")

        late = max(10, args.events // 100)
        log(f"sending {late:,} late events for {p1} and {args.events // 10:,} events for {p2}")
        out["late_ingest"] = await ingest(
            client,
            generate(
                late,
                0.0,
                0.0,
                args.customers,
                args.seed + 1,
                datetime.now(UTC),
                period_start=start1,
                id_prefix="late",
            ),
            args,
        )
        out["next_ingest"] = await ingest(
            client,
            generate(
                args.events // 10,
                0.10,
                0.0,
                args.customers,
                args.seed + 2,
                datetime.now(UTC),
                period_start=start2,
                id_prefix="next",
            ),
            args,
        )
        await asyncio.sleep(args.cutoff_lag + 1)
        log(f"closing {p2}")
        out["close_2"] = await close_chain(client, p2)
        log(f"{p2}: {out['close_2']}")

        invoices = (await client.get("/v1/invoices", params={"period": p2})).json()
        sample = next(i for i in invoices)
        inv = (await client.get(f"/v1/invoices/{sample['id']}")).json()
        adj = [ln for ln in inv["lines"] if ln["kind"] == "adjustment"]
        out["sample_invoice"] = {
            "id": inv["id"],
            "total_cents": inv["total_cents"],
            "adjustment_lines": adj,
            "content_hash": inv["content_hash"],
        }
        sf_total = 0
        for p in (p1, p2):
            r = (await client.get(f"/v1/periods/{p}/reconciliation")).json()
            sf_total += r["salesforce_total_cents"]
        periods = (await client.get("/v1/periods")).json()
        out["totals"] = {
            "ledger_cents": sum(int(p["total_cents"]) for p in periods),
            "salesforce_cents": sf_total,
            "drift": [p["drift"] for p in periods],
        }
        log(f"totals: {out['totals']}")
        out["rerate"] = {}
        for p in (p1, p2):
            r = (await client.get(f"/v1/periods/{p}/rerate")).json()
            out["rerate"][p] = {k: r[k] for k in ("invoices", "identical", "delta_cents")}
        log(f"re-rate from raw events: {out['rerate']}")

    if args.dlq:
        out["dlq"] = await dlq_round_trip(args, p1)
        log(f"DLQ round trip: {out['dlq']}")

    if args.replay:
        log("replaying the full event log twice into a scratch database")
        out["replay"] = json.loads(
            compose(args, "api", "metering", "replay-check", "--scratch-url", SCRATCH_URL)
        )
        log(
            f"replay: identical_across_runs={out['replay']['identical_across_runs']} "
            f"all_match_stored={out['replay']['all_match_stored']}"
        )
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--url", default=os.environ.get("API_URL", "http://localhost:8001"))
    p.add_argument("--events", type=int, default=100_000)
    p.add_argument("--period", default="2026-08")
    p.add_argument("--customers", type=int, default=100)
    p.add_argument("--batch-size", type=int, default=1000)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--cutoff-lag", type=float, default=3.0, help="CLOSE_CUTOFF_LAG_SECONDS")
    p.add_argument("--fail-sync", action="store_true")
    p.add_argument("--fail-calls", type=int, default=3)
    p.add_argument("--replay", action="store_true")
    p.add_argument("--dlq", action="store_true", help="also run the DLQ round trip")
    p.add_argument("--jobq-url", default=os.environ.get("JOBQ_API_URL", "http://localhost:8002"))
    p.add_argument("--compose", default="docker compose")
    p.add_argument("--out")
    args = p.parse_args()
    result = asyncio.run(run(args))
    checks = {
        "aggregate_matches_raw": result["aggregate_check"],
        "zero_drift": all(d == 0 for d in result["totals"]["drift"]),
        "ledger_equals_salesforce": result["totals"]["ledger_cents"]
        == result["totals"]["salesforce_cents"],
        "rerate_identical": all(r["identical"] == r["invoices"] for r in result["rerate"].values()),
        "late_events_adjusted": bool(result["close_2"]["close"]["adjustment_lines"]),
    }
    if args.fail_sync:
        checks["sync_retried_by_jobq"] = result["close_1"]["sync_attempts"] >= 2
    if args.dlq:
        d = result["dlq"]
        checks["dlq_round_trip"] = (
            d["drift_after_tamper"] == 1
            and d["in_dlq"]
            and d["dead_status"] == "dead"
            and d["replayed_status"] == "succeeded"
            and d["drift_after_replay"] == 0
        )
    if args.replay:
        checks["replay_identical"] = (
            result["replay"]["identical_across_runs"] and result["replay"]["all_match_stored"]
        )
    result["checks"] = checks
    text = json.dumps(result, indent=2, default=str)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
    print(json.dumps(checks, indent=2))
    if not all(checks.values()):
        raise SystemExit("demo checks failed")


if __name__ == "__main__":
    main()
