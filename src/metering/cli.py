"""Command line: `metering <command>` (or `python -m metering.cli`).

seed-plans                      insert the default price plans (idempotent)
aggregate [--loop --interval S] roll new events into usage_hourly
close-period YYYY-MM            close a period and write invoices (in-process, no queue)
sync YYYY-MM [--force]          push a period to Salesforce
reconcile YYYY-MM [--repair]    compare ledger with Salesforce
rerate YYYY-MM                  recompute a period from raw events and diff
replay-check --scratch-url URL  copy the event log into a scratch DB, rebuild every
                                invoice, compare content hashes
fake-sf fail-next N [--after M] | fake-sf tamper ... | fake-sf summary
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import text

from metering.aggregation import aggregate_dirty
from metering.config import get_settings
from metering.invoicing import close_period, rerate_period
from metering.pricing import seed_default_plans
from metering.replay import replay_check
from metering.runtime import Runtime
from metering.salesforce.client import FakeSalesforceClient
from metering.salesforce.sync import reconcile_period, sync_period


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


async def _run(args: argparse.Namespace) -> int:
    rt = Runtime.build()
    s = rt.settings
    try:
        match args.cmd:
            case "seed-plans":
                async with rt.sessions() as session, session.begin():
                    _print({"inserted": await seed_default_plans(session)})
            case "aggregate":
                while True:
                    t0 = time.perf_counter()
                    async with rt.sessions() as session, session.begin():
                        res = await aggregate_dirty(
                            session, overlap=timedelta(seconds=s.aggregate_overlap_seconds)
                        )
                    took = time.perf_counter() - t0
                    if not args.loop or res.dirty_cells:
                        print(
                            json.dumps(
                                {
                                    "dirty_cells": res.dirty_cells,
                                    "rows_changed": res.rows_changed,
                                    "seconds": round(took, 3),
                                }
                            ),
                            flush=True,
                        )
                    if not args.loop:
                        break
                    await asyncio.sleep(max(0.0, args.interval - took))
            case "close-period":
                async with rt.sessions() as session, session.begin():
                    r = await close_period(
                        session,
                        args.period,
                        cutoff_lag=timedelta(seconds=s.close_cutoff_lag_seconds),
                    )
                _print(r)
            case "sync":
                synced = await sync_period(
                    rt.sessions,
                    rt.salesforce(),
                    args.period,
                    force=args.force,
                    attempts=s.sync_attempts,
                    backoff_base=s.sync_backoff_base,
                )
                _print(synced.as_dict())
            case "reconcile":
                report = await reconcile_period(
                    rt.sessions, rt.salesforce(), args.period, repair=args.repair
                )
                _print(report.as_dict())
                return 0 if report.drift == 0 else 3
            case "rerate":
                async with rt.sessions() as session:
                    diffs = await rerate_period(session, args.period)
                changed = [d.as_dict() for d in diffs if not d.identical]
                _print({"invoices": len(diffs), "changed": len(changed), "diffs": changed[:50]})
            case "replay-check":
                _print(await replay_check(rt, args.scratch_url, runs=args.runs, seed=args.seed))
            case "fake-sf":
                fake = FakeSalesforceClient(s.fake_sf_state)
                if args.fake_cmd == "fail-next":
                    fake.fail_next(args.n, after=args.after)
                    _print({"scheduled_failures": args.n, "after": args.after})
                elif args.fake_cmd == "clear-failures":
                    fake.clear_failures()
                    _print({"scheduled_failures": 0})
                elif args.fake_cmd == "tamper":
                    fake.tamper(
                        args.sobject, args.ext_field, args.ext_id, **{args.field: args.value}
                    )
                    _print({"tampered": args.ext_id})
                else:
                    _print(
                        {o: fake.count(o) for o in ("Account", "Usage_Summary__c", "Invoice__c")}
                    )
            case "wait-db":
                deadline = time.monotonic() + args.timeout
                while True:
                    try:
                        async with rt.engine.connect() as conn:
                            await conn.execute(text("SELECT 1"))
                        break
                    except Exception:
                        if time.monotonic() > deadline:
                            raise
                        await asyncio.sleep(1)
        return 0
    finally:
        await rt.aclose()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="metering", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seed-plans")
    agg = sub.add_parser("aggregate")
    agg.add_argument("--loop", action="store_true")
    agg.add_argument("--interval", type=float, default=1.0)
    for name in ("close-period", "rerate"):
        sub.add_parser(name).add_argument("period")
    sy = sub.add_parser("sync")
    sy.add_argument("period")
    sy.add_argument("--force", action="store_true")
    rc = sub.add_parser("reconcile")
    rc.add_argument("period")
    rc.add_argument("--repair", action="store_true")
    rp = sub.add_parser("replay-check")
    rp.add_argument("--scratch-url", required=True)
    rp.add_argument("--runs", type=int, default=2)
    rp.add_argument("--seed", type=int, default=1)
    w = sub.add_parser("wait-db")
    w.add_argument("--timeout", type=float, default=60)
    fk = sub.add_parser("fake-sf")
    fsub = fk.add_subparsers(dest="fake_cmd", required=True)
    fn = fsub.add_parser("fail-next")
    fn.add_argument("n", type=int)
    fn.add_argument("--after", type=int, default=0)
    t = fsub.add_parser("tamper")
    for a in ("sobject", "ext_field", "ext_id", "field", "value"):
        t.add_argument(a)
    fsub.add_parser("clear-failures")
    fsub.add_parser("summary")
    args = p.parse_args(argv)
    _ = get_settings()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
