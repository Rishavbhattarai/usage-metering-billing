"""Integration fixtures: a real Postgres, migrated with Alembic.

TEST_DATABASE_URL selects the database (it is wiped). If it is unreachable the tests are
skipped, unless REQUIRE_DB=1 (set in CI) in which case they fail.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from metering.api.app import create_app
from metering.config import Settings
from metering.pricing import seed_default_plans
from metering.runtime import Runtime
from metering.salesforce.client import FakeSalesforceClient

ROOT = Path(__file__).resolve().parents[2]
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://metering:metering@localhost:5433/metering_test"
)

pytestmark = pytest.mark.integration


async def _ping(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def database_url() -> str:
    try:
        asyncio.run(asyncio.wait_for(_ping(TEST_DATABASE_URL), timeout=5))
    except Exception as exc:
        msg = f"Postgres not reachable at TEST_DATABASE_URL ({exc!r})"
        if os.environ.get("REQUIRE_DB") == "1":
            pytest.fail(msg)
        pytest.skip(msg)

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    cfg.attributes["configure_logger"] = False
    # Round-trip the migrations so downgrade() is exercised too.
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    return TEST_DATABASE_URL


ALL_TABLES = (
    "usage_events, usage_hourly, aggregation_state, price_plans, billing_periods, invoices, "
    "invoice_lines, sf_sync_log, reconciliation_runs"
)


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        max_batch_size=1000,
        close_cutoff_lag_seconds=0,
        aggregate_overlap_seconds=0,
        sync_backoff_base=0.001,
        jobq_url=None,
        salesforce_mode="fake",
        fake_sf_state=None,
    )


@pytest.fixture
async def rt(settings: Settings) -> AsyncIterator[Runtime]:
    runtime = Runtime.build(settings)
    runtime.use_salesforce(FakeSalesforceClient())
    async with runtime.engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {ALL_TABLES}"))
    async with runtime.sessions() as session, session.begin():
        await seed_default_plans(session)
    yield runtime
    await runtime.aclose()


@pytest.fixture
def fake_sf(rt: Runtime) -> FakeSalesforceClient:
    sf = rt.salesforce()
    assert isinstance(sf, FakeSalesforceClient)
    return sf


@pytest.fixture
async def app(rt: Runtime) -> FastAPI:
    return create_app(runtime=rt)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(scope="session")
def scratch_database_url(database_url: str) -> str:
    """A second, empty database for replay checks (created if missing)."""
    base, _, name = database_url.rpartition("/")
    scratch = f"{base}/{name}_replay"

    async def create() -> None:
        engine = create_async_engine(database_url, isolation_level="AUTOCOMMIT")
        try:
            async with engine.connect() as conn:
                exists = await conn.scalar(
                    text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": f"{name}_replay"}
                )
                if not exists:
                    await conn.execute(text(f'CREATE DATABASE "{name}_replay"'))
        finally:
            await engine.dispose()

    asyncio.run(create())
    return scratch
