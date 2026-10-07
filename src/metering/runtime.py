"""Process-wide dependencies (DB engine, Salesforce client, job queue), built lazily from
Settings so the same code runs in the API, a jobq worker and the CLI."""

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from metering.config import Settings, get_settings
from metering.db import make_engine, make_sessionmaker
from metering.jobs.queue import InProcessJobQueue, JobQueue, Project1JobQueue
from metering.salesforce.client import (
    FakeSalesforceClient,
    SalesforceClient,
    SimpleSalesforceClient,
)


@dataclass
class Runtime:
    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    _sf: SalesforceClient | None = None
    _queue: JobQueue | None = field(default=None)

    @classmethod
    def build(cls, settings: Settings | None = None) -> "Runtime":
        settings = settings or get_settings()
        engine = make_engine(settings.database_url, pool_size=settings.db_pool_size)
        return cls(settings, engine, make_sessionmaker(engine))

    def salesforce(self) -> SalesforceClient:
        if self._sf is None:
            if self.settings.salesforce_mode == "live":
                self._sf = SimpleSalesforceClient.from_env()
            else:
                self._sf = FakeSalesforceClient(self.settings.fake_sf_state)
        return self._sf

    def use_salesforce(self, client: SalesforceClient) -> None:
        self._sf = client

    def queue(self) -> JobQueue:
        """jobq when JOBQ_URL is set, else an in-process queue running the billing jobs."""
        if self._queue is None:
            if self.settings.jobq_url:
                self._queue = Project1JobQueue.from_jobq(self.settings.jobq_url)
            else:
                from metering.jobs.handlers import JOBS, bind

                q = InProcessJobQueue()
                for name, fn in JOBS.items():
                    q.register(name, bind(fn, lambda: self, lambda: q))
                self._queue = q
        return self._queue

    async def aclose(self) -> None:
        await self.engine.dispose()


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = Runtime.build()
    return _runtime
