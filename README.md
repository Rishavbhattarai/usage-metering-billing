# Usage Metering & Billing Pipeline

Turns raw usage events (API calls, GB stored) into monthly invoices you can audit, and
syncs customers and invoices to Salesforce. Every cent can be recomputed from the raw events.

Project 3 of a 3-project portfolio. Full scope: [SCOPE.md](SCOPE.md). Design:
[docs/design.md](docs/design.md). Decisions: [docs/adr/](docs/adr/).

<!-- CI badge: add once the repo is on GitHub:
![CI](https://github.com/<you>/usage-billing-pipeline/actions/workflows/ci.yml/badge.svg) -->

## Status

| Week | Deliverable | State |
|------|-------------|-------|
| 0 | Salesforce dev org, `sf` CLI, Connected App | Metadata + guide ready ([docs/salesforce-setup.md](docs/salesforce-setup.md)). You do the org signup |
| 1 | Ingestion API, append-only event store, dedupe | **Done.** 10k events with 10% dupes give exactly 9k rows (integration test) |
| 2 | Aggregation, price plans, rating engine | Rating engine (flat, graduated, minimum commit) with unit + Hypothesis tests. Aggregator: push 2/3 |
| 3 | Invoices on Project 1's queue, late events | Not started. `JobQueue` interface + in-process implementation ready |
| 4 | Salesforce sync + reconciliation | `SalesforceClient` interface + in-memory fake. Real client: push 3/3 |
| 5 | Load test, re-rating demo, results | Not started |

## Quickstart

```bash
cp .env.example .env
docker compose up -d --build          # Postgres 16 (host port 5433) + API (port 8000)

curl -s localhost:8000/v1/events/batch -H 'content-type: application/json' -d '{"events": [
  {"event_id":"e1","customer_id":"c1","meter":"api_calls","quantity":"10","occurred_at":"2026-10-01T10:00:00Z"},
  {"event_id":"e1","customer_id":"c1","meter":"api_calls","quantity":"10","occurred_at":"2026-10-01T10:00:00Z"}
]}'
# {"received":2,"accepted":1,"duplicates":1}

python loadtest/generate_events.py --count 100000 --url http://localhost:8000
docker compose down                   # add -v to drop the data volume
```

### API

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/v1/events` | One event. Returns `{received, accepted, duplicates}` |
| `POST` | `/v1/events/batch` | `{"events": [...]}`, up to `MAX_BATCH_SIZE` (default 5000). Atomic. Safe to retry |
| `GET`  | `/v1/events/{event_id}` | Stored event |
| `GET`  | `/healthz` | Checks the DB connection |

Event: `event_id` (yours, the idempotency key), `customer_id`, `meter`, `quantity`
(decimal, at most 6 dp, sent as a string ideally), `occurred_at` (ISO 8601 *with* a timezone).
OpenAPI docs: <http://localhost:8000/docs>.

## Development

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -e ".[dev]"
# (or: python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]")

.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
docker compose up -d postgres         # integration tests use TEST_DATABASE_URL (db metering_test)
.venv/bin/pytest -q                   # integration tests skip if Postgres is unreachable
```

Migrations: `alembic upgrade head` (uses `DATABASE_URL`).

## Layout

```
src/metering/
  api/app.py          FastAPI app (uvicorn --factory metering.api.app:create_app)
  ingestion.py        INSERT ... ON CONFLICT DO NOTHING, accepted/duplicate counts
  rating/             pure rating engine: money.py, plans.py, engine.py
  aggregation.py      hourly rollup (stub: push 2/3)
  invoicing.py        TODO week 3
  jobs/queue.py       JobQueue protocol, InProcessJobQueue, Project1JobQueue (HTTP adapter)
  salesforce/         SalesforceClient protocol, FakeSalesforceClient, real client stub (push 3/3)
migrations/           Alembic (usage_events + append-only trigger, usage_hourly)
salesforce/           SFDX project: custom objects, External IDs, permission set
tests/unit            rating (examples + Hypothesis), job queue, SF fake, schemas
tests/integration     ingestion against real Postgres (10k / 10% dupes milestone)
loadtest/             event generator with duplicates and late events
chaos/                planned failure demos
docs/                 design.md, adr/, salesforce-setup.md
```

## Dependency on Project 1

Invoice, sync and reconciliation jobs will run on
[Project 1's job queue](../01-distributed-job-queue) (`jobq`). Code depends only on the
`JobQueue` protocol. `InProcessJobQueue` is the default. `Project1JobQueue` adapts
`jobq.client.AsyncJobqClient` and isn't installed here yet. To switch:

```bash
pip install -e ../01-distributed-job-queue
export JOBQ_URL=http://localhost:8001     # jobq defaults to :8000, which this API also uses
```
```python
queue = Project1JobQueue.from_jobq()
```

Handlers will register with jobq's `@registry.handler("name")` and must be idempotent
(delivery is at-least-once).

## Results

Not measured yet (Week 5): ingestion throughput and p50/p99, event-to-aggregate lag,
replay determinism (invoice hash), and reconciliation drift after a forced failure.
