# ADR 0003: Late events become adjustments on the next invoice

- Status: Accepted
- Date: 2026-10-07

## Context

Usage for a month keeps arriving after the month ends (retries, offline producers). Once an
invoice is issued it must not change, but late usage still has to be billed, exactly once,
and every invoice must be reproducible from the raw event log.

## Decision

- Closing period P records a **cutoff** `T_P` in `billing_periods`. P's invoices bill events
  that occurred in P and were received at or before `T_P`. Events for P that arrive before the
  close are accepted and billed normally (the grace period is however long you wait to close).
- An event that occurred in an earlier period Q but was received in `(T_prev, T_P]` is
  **late**. It goes on P's invoice as an adjustment line for Q. Closed invoices are never
  reopened.
- The adjustment is priced at Q's marginal rate using Q's plan:
  `charge(Q, billed + late) - charge(Q, billed)`, where `billed` is Q's quantity received up
  to `T_prev`. Late usage that pushes a customer into a cheaper tier is priced in that tier,
  late usage under a minimum commit costs $0, and nothing is billed twice.
- Periods close strictly in order, one invoice per (customer, period)
  (`UNIQUE(customer_id, period)`), and the close is one transaction. Closing a period twice
  returns the stored result.
- The cutoff is `now() - CLOSE_CUTOFF_LAG_SECONDS` (default 60). An ingest transaction
  still in flight when the close starts has a `received_at` before `now()` but is not yet
  visible, so a cutoff of `now()` could miss it for good. With the lag, it lands after the
  cutoff and is billed as late usage next period.
- Invoices are computed from **raw events** with these cutoffs, not from hourly rollups,
  because rollups lose `received_at`.

## Consequences

- An invoice is a pure function of (events, cutoffs, plans). Re-rating recomputes and diffs
  it, and replaying the event log reproduces it byte for byte. In the 1M-event demo, two
  replays into a scratch database rebuilt all 200 invoices with identical SHA-256 hashes.
- Customers see late usage as a separate line naming the period it belongs to.
- A customer with no usage in a period gets no invoice. Minimum commits only apply to meters
  with usage. A subscriptions model would change this.

## Alternatives considered

- **Reopen and reissue the old invoice.** Simpler arithmetic, but invoices change after
  they're sent, and Salesforce and the ledger need versioning. Rejected.
- **Reject late events.** Loses revenue and makes producers' retries unsafe. Rejected.
