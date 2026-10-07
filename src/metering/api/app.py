"""FastAPI app: ingestion endpoints.

Run: uvicorn --factory metering.api.app:create_app --host 0.0.0.0 --port 8000
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from metering import __version__
from metering.config import Settings, get_settings
from metering.db import make_engine, make_sessionmaker
from metering.ingestion import ingest_events
from metering.models import UsageEvent
from metering.schemas import BatchIn, IngestResult, UsageEventIn, UsageEventOut


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    maker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with maker() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    # The engine is created eagerly (it does not connect until first use) so the app also
    # works under test transports that skip lifespan events.
    engine = make_engine(settings.database_url, pool_size=settings.db_pool_size)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app = FastAPI(title="Usage Metering & Billing", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)

    @app.get("/healthz")
    async def healthz(session: Session) -> dict[str, str]:
        await session.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.post("/v1/events", response_model=IngestResult)
    async def ingest_one(event: UsageEventIn, session: Session) -> IngestResult:
        """Ingest one event. Safe to retry: a repeated event_id reports duplicates=1."""
        async with session.begin():
            return await ingest_events(session, [event])

    @app.post("/v1/events/batch", response_model=IngestResult)
    async def ingest_batch(batch: BatchIn, session: Session) -> IngestResult:
        """Ingest a batch atomically (all rows or none). Safe to retry the whole batch."""
        if len(batch.events) > settings.max_batch_size:
            raise HTTPException(
                413,
                f"batch has {len(batch.events)} events; max is {settings.max_batch_size}",
            )
        async with session.begin():
            return await ingest_events(session, batch.events)

    @app.get("/v1/events/{event_id}", response_model=UsageEventOut)
    async def get_event(event_id: str, session: Session) -> UsageEvent:
        event = await session.get(UsageEvent, event_id)
        if event is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")
        return event

    return app
