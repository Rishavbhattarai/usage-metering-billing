"""Runtime configuration, read from environment variables (see .env.example)."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://metering:metering@localhost:5433/metering"
    # Upper bound on events per batch request. Keeps a single transaction (and its row
    # locks) short and well under Postgres' 32767 bind-parameter limit per statement.
    max_batch_size: int = 5000
    db_pool_size: int = 10

    # Jobs. With JOBQ_URL set (and the jobq package installed) jobs go to Project 1's queue;
    # otherwise they run in-process.
    jobq_url: str | None = None

    # Salesforce: "fake" (in-memory, optionally persisted to fake_sf_state) or "live".
    salesforce_mode: Literal["fake", "live"] = "fake"
    fake_sf_state: str | None = None
    sync_attempts: int = 3
    sync_backoff_base: float = 0.5

    # Billing. See docs/adr/0003-late-events.md.
    close_cutoff_lag_seconds: float = 60.0
    # Aggregator re-scan window. See docs/adr/0002-aggregation-strategy.md.
    aggregate_overlap_seconds: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
