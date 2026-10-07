"""Runtime configuration, read from environment variables (see .env.example)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://metering:metering@localhost:5433/metering"
    # Upper bound on events per batch request. Keeps a single transaction (and its row
    # locks) short and well under Postgres' 32767 bind-parameter limit per statement.
    max_batch_size: int = 5000
    db_pool_size: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()
