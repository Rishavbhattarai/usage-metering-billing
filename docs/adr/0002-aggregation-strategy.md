# ADR 0002: Recompute whole hour cells from raw events

- Status: Accepted
- Date: 2026-10-07

## Context

The aggregator rolls `usage_events` into `usage_hourly` (customer, meter, UTC hour). Events
arrive late and duplicated, and the aggregator itself can crash or run twice. Hourly totals
feed the usage dashboard and the event-to-aggregate lag metric.

## Decision

- A *cell* is (customer_id, meter, UTC hour). Each run finds the cells touched by events
  received since the last run, then recomputes each of those cells from **all** of its raw
  events with one `INSERT ... SELECT ... GROUP BY ... ON CONFLICT DO UPDATE`.
- The watermark is the run's transaction time, stored in `aggregation_state`. Each run
  re-scans `AGGREGATE_OVERLAP_SECONDS` (default 30) before the watermark. An event's
  `received_at` is its ingest transaction's start time, so an ingest transaction that
  commits after a run started can carry a `received_at` older than the watermark. The overlap
  catches it, and recomputing a clean cell changes nothing.
- Hour buckets are truncated in UTC explicitly. `date_trunc` on a `timestamptz` uses the
  session time zone, and a CHECK constraint rejects non-UTC-aligned hours.
- Daily usage is a view over `usage_hourly`: at most 24 rows per cell, and it can never
  disagree with the hourly table.
- `ANALYZE` runs on the temp table of dirty cells before the upsert.

## Why recompute instead of adding increments

Adding only new events (`quantity = quantity + delta`) is faster per run, but it is only
correct if every event is added exactly once. That needs exact, gap-free tracking of which
events were already added, and the watermark race above makes that hard. Recomputing is
idempotent: running it twice, or after a crash, gives the same rows. Tests check this,
including the 10:59:59.999 / 11:00:00 boundary and duplicate events.

## Consequences

- Cost per run grows with the number of events in the dirty cells, not just new events. In
  the 1M-event load test, every hour of the month was dirty on every pass, and a pass took
  up to 4.6 s at about 900k rows. Event-to-aggregate lag was p50 2.2 s and p95 5.6 s during
  that load, and p50 0.5 s with no load (`loadtest/results/`).
- Without `ANALYZE` the planner had no statistics for the temp table and joined each dirty
  cell against every event of its customer: 205 s for one pass at 900k events, against 2.1 s
  analyzed (measured with `EXPLAIN ANALYZE`).
- Invoices do **not** read `usage_hourly` (see ADR 0003). Rollups are a derived cache for the
  dashboard and can be rebuilt at any time with `aggregate_range`.

## At 10x

Partition `usage_events` by month, and track dirty cells in a small table written at ingest
time instead of scanning `received_at`. Both keep a pass proportional to the new data.
