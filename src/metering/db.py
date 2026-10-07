"""Async engine / session helpers."""

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


def make_engine(database_url: str, pool_size: int = 10) -> AsyncEngine:
    return create_async_engine(database_url, pool_size=pool_size, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:  # type: ignore[type-arg]
    return async_sessionmaker(engine, expire_on_commit=False)
