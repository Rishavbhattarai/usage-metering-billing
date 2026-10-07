import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from metering.jobs import InProcessJobQueue, JobQueue, Project1JobQueue
from metering.jobs.queue import Payload, Priority


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


async def test_in_process_queue_runs_job_and_reports_result() -> None:
    q = InProcessJobQueue()
    seen: list[Payload] = []

    async def handler(payload: Payload) -> str:
        seen.append(payload)
        return "ok"

    q.register("close_period", handler)
    assert isinstance(q, JobQueue)
    job_id = await q.enqueue("close_period", {"period": "2026-09"})
    job = await q.get(job_id)
    assert job is not None
    assert (job.status, job.result, job.attempts) == ("succeeded", "ok", 1)
    assert seen == [{"period": "2026-09"}]


async def test_idempotency_key_returns_original_job_without_rerunning() -> None:
    q = InProcessJobQueue()
    runs = 0

    async def handler(_: Payload) -> None:
        nonlocal runs
        runs += 1

    q.register("invoice", handler)
    a = await q.enqueue("invoice", {"c": 1}, idempotency_key="invoice:cust_1:2026-09")
    b = await q.enqueue("invoice", {"c": 1}, idempotency_key="invoice:cust_1:2026-09")
    assert a == b
    assert runs == 1


async def test_failed_job_is_recorded_not_raised() -> None:
    q = InProcessJobQueue()

    async def boom(_: Payload) -> None:
        raise RuntimeError("salesforce down")

    q.register("sf_sync", boom)
    job = await q.get(await q.enqueue("sf_sync", {}))
    assert job is not None
    assert job.status == "failed"
    assert job.error is not None
    assert "salesforce down" in job.error


async def test_scheduled_job_runs_when_due() -> None:
    clock = Clock()
    q = InProcessJobQueue(clock=clock)

    async def handler(_: Payload) -> int:
        return 1

    q.register("close_period", handler)
    job_id = await q.enqueue("close_period", {}, run_at=clock.now + timedelta(hours=1))
    assert (await q.get(job_id)).status == "queued"  # type: ignore[union-attr]
    assert await q.run_due() == 0
    clock.now += timedelta(hours=1)
    assert await q.run_due() == 1
    assert (await q.get(job_id)).status == "succeeded"  # type: ignore[union-attr]


async def test_unknown_job_type_is_rejected() -> None:
    with pytest.raises(KeyError):
        await InProcessJobQueue().enqueue("nope", {})


@dataclass(frozen=True)
class FakeJobqJob:
    """Same shape as jobq.client.Job (only the fields the adapter reads)."""

    id: uuid.UUID
    type: str
    payload: dict[str, Any]
    status: str
    attempts: int
    result: Any
    last_error: str | None


class JobNotFound(Exception):
    pass


class FakeAsyncJobqClient:
    """Mimics jobq.client.AsyncJobqClient: idempotency keys, JobNotFoundError on miss."""

    def __init__(self) -> None:
        self.jobs: dict[str, FakeJobqJob] = {}
        self.by_key: dict[str, FakeJobqJob] = {}
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
        priority: Priority = "normal",
        run_at: datetime | None = None,
        max_attempts: int | None = None,
    ) -> FakeJobqJob:
        self.calls.append(
            {
                "job_type": job_type,
                "payload": payload,
                "priority": priority,
                "run_at": run_at,
                "max_attempts": max_attempts,
            }
        )
        if idempotency_key in self.by_key:
            return self.by_key[idempotency_key]
        job = FakeJobqJob(uuid.uuid4(), job_type, payload or {}, "queued", 0, None, None)
        self.jobs[str(job.id)] = job
        if idempotency_key:
            self.by_key[idempotency_key] = job
        return job

    async def get(self, job_id: uuid.UUID | str) -> FakeJobqJob:
        try:
            return self.jobs[str(job_id)]
        except KeyError:
            raise JobNotFound(job_id) from None

    async def aclose(self) -> None:
        self.closed = True


async def test_project1_adapter_maps_jobq_client() -> None:
    fake = FakeAsyncJobqClient()
    q = Project1JobQueue(fake, not_found_errors=(JobNotFound,), max_attempts=5)
    assert isinstance(q, JobQueue)

    run_at = datetime(2026, 10, 1, tzinfo=UTC)
    job_id = await q.enqueue(
        "invoice", {"period": "2026-09"}, idempotency_key="k1", priority="high", run_at=run_at
    )
    again = await q.enqueue("invoice", {"period": "2026-09"}, idempotency_key="k1")
    assert again == job_id
    assert fake.calls[0] == {
        "job_type": "invoice",
        "payload": {"period": "2026-09"},
        "priority": "high",
        "run_at": run_at,
        "max_attempts": 5,
    }

    job = await q.get(job_id)
    assert job is not None
    assert (job.id, job.job_type, job.status, job.payload) == (
        job_id,
        "invoice",
        "queued",
        {"period": "2026-09"},
    )
    assert await q.get(str(uuid.uuid4())) is None
    await q.aclose()
    assert fake.closed


async def test_naive_run_at_is_rejected_like_jobq() -> None:
    q = InProcessJobQueue()

    async def handler(_: Payload) -> None:
        return None

    q.register("x", handler)
    with pytest.raises(ValueError, match="timezone-aware"):
        await q.enqueue("x", {}, run_at=datetime(2026, 10, 1))  # noqa: DTZ001
    with pytest.raises(ValueError, match="timezone-aware"):
        await Project1JobQueue(FakeAsyncJobqClient()).enqueue(
            "x",
            {},
            run_at=datetime(2026, 10, 1),  # noqa: DTZ001
        )


def test_from_jobq_needs_the_package() -> None:
    try:
        import jobq  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        with pytest.raises(ImportError):
            Project1JobQueue.from_jobq("http://localhost:8001")
    else:
        pytest.skip("jobq is installed; covered by Project 1's own tests")
