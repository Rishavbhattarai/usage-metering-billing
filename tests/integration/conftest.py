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


@pytest.fixture
async def app(database_url: str) -> AsyncIterator[FastAPI]:
    app = create_app(Settings(database_url=database_url, max_batch_size=1000))
    async with app.state.engine.begin() as conn:
        await conn.execute(text("TRUNCATE usage_events, usage_hourly"))
    yield app
    await app.state.engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
