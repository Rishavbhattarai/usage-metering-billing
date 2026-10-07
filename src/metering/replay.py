"""Replay the full event log into a scratch database and rebuild every invoice.

Proves determinism end to end: the raw events (with their received_at), the price plans and
the period cutoffs are copied, in a different shuffled order each run, into an empty database;
every closed period is recomputed; and the content hashes are compared with the stored
invoices and across runs.
"""

import asyncio
import hashlib
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import asyncpg
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from metering.db import make_sessionmaker
from metering.invoicing import compute_invoices

if TYPE_CHECKING:
    from metering.runtime import Runtime

CHUNK = 50_000
EVENT_COLS = ("event_id", "customer_id", "meter", "quantity", "occurred_at", "received_at")
PLAN_COLS = ("id", "meter", "model", "unit_price", "tiers", "min_commit_cents", "effective_from")


def _dsn(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _alembic_config(url: str) -> Config:
    ini = Path(os.environ.get("ALEMBIC_CONFIG", "alembic.ini")).resolve()
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    return cfg


async def _copy_log(src: asyncpg.Connection, dst: asyncpg.Connection, salt: str) -> int:
    await dst.execute(
        "TRUNCATE usage_events, price_plans, billing_periods, invoices, invoice_lines CASCADE"
    )
    plans = await src.fetch(f"SELECT {', '.join(PLAN_COLS)} FROM price_plans")
    await dst.copy_records_to_table("price_plans", records=plans, columns=PLAN_COLS)
    periods = await src.fetch("SELECT period, closed_at FROM billing_periods")
    await dst.copy_records_to_table(
        "billing_periods", records=periods, columns=("period", "closed_at")
    )
    copied = 0
    async with src.transaction():
        cursor = src.cursor(
            f"SELECT {', '.join(EVENT_COLS)} FROM usage_events ORDER BY md5(event_id || $1)",
            salt,
            prefetch=CHUNK,
        )
        batch: list[Any] = []
        async for row in cursor:
            batch.append(tuple(row))
            if len(batch) == CHUNK:
                await dst.copy_records_to_table("usage_events", records=batch, columns=EVENT_COLS)
                copied += len(batch)
                batch = []
        if batch:
            await dst.copy_records_to_table("usage_events", records=batch, columns=EVENT_COLS)
            copied += len(batch)
    await dst.execute("ANALYZE usage_events")
    return copied


async def replay_check(
    rt: "Runtime", scratch_url: str, runs: int = 2, seed: int = 1
) -> dict[str, Any]:
    if scratch_url == rt.settings.database_url:
        raise ValueError("scratch database must differ from the live one (it is wiped)")
    await asyncio.to_thread(command.upgrade, _alembic_config(scratch_url), "head")

    async with rt.sessions() as session:
        stored: dict[str, str] = dict(
            (await session.execute(text("SELECT id, content_hash FROM invoices"))).all()
        )
        periods = (
            await session.execute(text("SELECT period, closed_at FROM billing_periods ORDER BY 1"))
        ).all()

    src = await asyncpg.connect(_dsn(rt.settings.database_url))
    dst = await asyncpg.connect(_dsn(scratch_url))
    engine = create_async_engine(scratch_url)
    sessions = make_sessionmaker(engine)
    run_reports = []
    try:
        for run in range(1, runs + 1):
            t0 = time.perf_counter()
            copied = await _copy_log(src, dst, salt=f"{seed}:{run}")
            rebuilt: dict[str, str] = {}
            prev = None
            async with sessions() as session:
                for period, closed_at in periods:
                    for inv in await compute_invoices(session, period, closed_at, prev):
                        rebuilt[inv.id] = inv.content_hash
                    prev = closed_at
            mismatched = sorted(
                k for k in set(stored) | set(rebuilt) if stored.get(k) != rebuilt.get(k)
            )
            digest = hashlib.sha256(
                "".join(f"{k}={rebuilt[k]}\n" for k in sorted(rebuilt)).encode()
            ).hexdigest()
            run_reports.append(
                {
                    "run": run,
                    "events_replayed": copied,
                    "invoices_rebuilt": len(rebuilt),
                    "mismatched_vs_stored": len(mismatched),
                    "mismatched_ids": mismatched[:20],
                    "invoice_set_sha256": digest,
                    "seconds": round(time.perf_counter() - t0, 2),
                }
            )
    finally:
        await src.close()
        await dst.close()
        await engine.dispose()
    return {
        "periods": [p for p, _ in periods],
        "stored_invoices": len(stored),
        "runs": run_reports,
        "identical_across_runs": len({r["invoice_set_sha256"] for r in run_reports}) == 1,
        "all_match_stored": all(r["mismatched_vs_stored"] == 0 for r in run_reports),
    }
