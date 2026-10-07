"""JobQueue abstraction.

Invoice generation, the Salesforce sync and reconciliation run as jobs on Project 1's job queue
(github.com/Rishavbhattarai/distributed-job-queue, package `jobq`). This module defines the
small interface this project depends on, plus:

  * InProcessJobQueue: runs handlers in-process. The default for tests and local dev.
  * Project1JobQueue: adapter over `jobq.client.AsyncJobqClient`. It is typed structurally
    (no import of `jobq`), so the core package doesn't require it. To switch:

        pip install -e ".[jobq]"
        queue = Project1JobQueue.from_jobq()          # base URL from $JOBQ_URL

Semantics both implementations share (matching jobq): an idempotency_key returns the original
job; run_at must be timezone-aware; a job with a future run_at stays "queued" until due.
Handlers must be idempotent, because jobq delivery is at-least-once.
"""

import importlib
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, runtime_checkable

Priority = Literal["low", "normal", "high"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "dead"]
Payload = Mapping[str, Any]
Handler = Callable[[Payload], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class JobInfo:
    id: str
    job_type: str
    status: JobStatus
    payload: Payload = field(default_factory=dict)
    attempts: int = 0
    result: Any = None
    error: str | None = None


@runtime_checkable
class JobQueue(Protocol):
    async def enqueue(
        self,
        job_type: str,
        payload: Payload,
        *,
        idempotency_key: str | None = None,
        priority: Priority = "normal",
        run_at: datetime | None = None,
    ) -> str:
        """Enqueue a job and return its id. Re-enqueueing with the same idempotency_key
        returns the original job's id and does not create a second job."""
        ...

    async def get(self, job_id: str) -> JobInfo | None: ...


class InProcessJobQueue:
    """Synchronous stand-in: a due job runs to completion inside enqueue().

    Jobs with a future run_at stay 'queued' until run_due() is called. A handler
    exception marks the job failed (no retries here; retries/DLQ are Project 1's job).
    """

    def __init__(self, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._handlers: dict[str, Handler] = {}
        self._jobs: dict[str, JobInfo] = {}
        self._run_at: dict[str, datetime] = {}
        self._by_key: dict[str, str] = {}
        self._clock = clock

    def register(self, job_type: str, handler: Handler) -> None:
        self._handlers[job_type] = handler

    async def enqueue(
        self,
        job_type: str,
        payload: Payload,
        *,
        idempotency_key: str | None = None,
        priority: Priority = "normal",
        run_at: datetime | None = None,
    ) -> str:
        if idempotency_key is not None and idempotency_key in self._by_key:
            return self._by_key[idempotency_key]
        _check_run_at(run_at)
        if job_type not in self._handlers:
            raise KeyError(f"no handler registered for job_type={job_type!r}")
        job_id = str(uuid.uuid4())
        if idempotency_key is not None:
            self._by_key[idempotency_key] = job_id
        if run_at is not None and run_at > self._clock():
            self._jobs[job_id] = JobInfo(job_id, job_type, "queued", dict(payload))
            self._run_at[job_id] = run_at
        else:
            self._jobs[job_id] = JobInfo(job_id, job_type, "queued", dict(payload))
            await self._run(job_id)
        return job_id

    async def get(self, job_id: str) -> JobInfo | None:
        return self._jobs.get(job_id)

    async def run_due(self) -> int:
        """Run scheduled jobs whose run_at has passed. Returns how many ran."""
        now = self._clock()
        due = sorted((t, jid) for jid, t in self._run_at.items() if t <= now)
        for _, job_id in due:
            del self._run_at[job_id]
            await self._run(job_id)
        return len(due)

    async def _run(self, job_id: str) -> None:
        job = replace(self._jobs[job_id], status="running")
        self._jobs[job_id] = job
        try:
            result = await self._handlers[job.job_type](job.payload)
        except Exception as exc:
            self._jobs[job_id] = replace(
                job, status="failed", attempts=job.attempts + 1, error=repr(exc)
            )
        else:
            self._jobs[job_id] = replace(
                job, status="succeeded", attempts=job.attempts + 1, result=result
            )


def _check_run_at(run_at: datetime | None) -> None:
    if run_at is not None and run_at.tzinfo is None:
        raise ValueError("run_at must be timezone-aware (e.g. datetime.now(UTC))")


class JobqJob(Protocol):
    """The fields of `jobq.client.Job` this adapter reads."""

    @property
    def id(self) -> uuid.UUID: ...
    @property
    def type(self) -> str: ...
    @property
    def status(self) -> str: ...
    @property
    def payload(self) -> dict[str, Any]: ...
    @property
    def attempts(self) -> int: ...
    @property
    def result(self) -> Any: ...
    @property
    def last_error(self) -> str | None: ...


class AsyncJobqClientLike(Protocol):
    """Structural match for `jobq.client.AsyncJobqClient`."""

    async def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        priority: Priority = "normal",
        run_at: datetime | None = None,
        max_attempts: int | None = None,
    ) -> JobqJob: ...

    async def get(self, job_id: uuid.UUID | str) -> JobqJob: ...

    async def aclose(self) -> None: ...


class Project1JobQueue:
    """JobQueue backed by Project 1's `jobq` service, via its AsyncJobqClient."""

    def __init__(
        self,
        client: AsyncJobqClientLike,
        *,
        not_found_errors: tuple[type[Exception], ...] = (),
        max_attempts: int | None = None,
    ) -> None:
        self._client = client
        self._not_found = not_found_errors
        self._max_attempts = max_attempts

    @classmethod
    def from_jobq(cls, base_url: str | None = None, **kwargs: Any) -> "Project1JobQueue":
        """Build from the real `jobq` package (must be installed). URL defaults to $JOBQ_URL."""
        jobq_client = importlib.import_module("jobq.client")
        return cls(
            jobq_client.AsyncJobqClient(base_url),
            not_found_errors=(jobq_client.JobNotFoundError,),
            **kwargs,
        )

    async def enqueue(
        self,
        job_type: str,
        payload: Payload,
        *,
        idempotency_key: str | None = None,
        priority: Priority = "normal",
        run_at: datetime | None = None,
    ) -> str:
        _check_run_at(run_at)
        job = await self._client.enqueue(
            job_type,
            dict(payload),
            idempotency_key=idempotency_key,
            priority=priority,
            run_at=run_at,
            max_attempts=self._max_attempts,
        )
        return str(job.id)

    async def get(self, job_id: str) -> JobInfo | None:
        try:
            job = await self._client.get(job_id)
        except self._not_found:
            return None
        status: JobStatus = job.status  # type: ignore[assignment]
        return JobInfo(
            id=str(job.id),
            job_type=job.type,
            status=status,
            payload=job.payload,
            attempts=job.attempts,
            result=job.result,
            error=job.last_error,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
