# ADR 0001: Integer cents in the ledger, Decimal in code, round half-up once per line

- Status: Accepted
- Date: 2026-10-06

## Context

Invoices must be reproducible to the cent from the raw event log: replaying the log twice
must give byte-identical invoices. Binary floating point can't represent most decimal
fractions (`0.1 + 0.2 != 0.3`), and the error depends on the order of operations, so float
money math is neither exact nor reproducible. We also have to pick a rounding rule and,
more importantly, *where* rounding happens, because rounding in different places gives
different totals.

## Decision

1. **Types.**
   - Quantities and unit prices: `NUMERIC(20, 6)` in Postgres and `decimal.Decimal` in Python.
   - Money amounts (line amounts, invoice totals, minimum commits): `BIGINT` cents in Postgres
     and `int` cents in Python.
   - `float` is banned from money paths. A unit test fails if `float` appears in
     `metering/rating`, and the plan and engine constructors reject non-`Decimal` input.
2. **Exact arithmetic until the rounding point.** `quantity x unit_price` is computed in a
   local `decimal.Context` with 60 digits of precision and the `Inexact` trap on, so precision
   loss raises instead of happening silently. Process-global decimal context is never used.
3. **Round once per invoice line**, to cents, with **ROUND_HALF_UP** (0.005 becomes 0.01).
   For graduated pricing each tier is its own line, so each tier is rounded once. The invoice
   total is the sum of the rounded line amounts, so the lines always add up to the total
   the customer sees.
4. The minimum-commit shortfall is computed in integer cents
   (`max(0, min_commit - usage_total)`), so it never rounds.

## Why half-up and not banker's rounding (half-even)

Banker's rounding removes the upward bias you get when rounding *many* values, like
per-event amounts. We round a handful of lines per invoice, so that bias is at most half a
cent per line and is bounded and visible. Half-up is what customers, finance teams and
spreadsheets expect, which makes invoices easier to explain and reconcile by hand.

## Consequences

- Property tests pin the behaviour: totals are never negative, are monotonic in quantity,
  flat pricing equals single-tier graduated pricing, and the rounding error is at most half a
  cent per line.
- Per-line rounding means the total can differ by up to 0.5 cent x (number of lines) from
  rounding the exact grand total once. That is accepted and documented.
- Salesforce `Invoice__c.Total__c` is `Currency(16, 2)`, written as `cents / 100`. The
  conversion is exact, so reconciliation compares integer cents.
- Unit prices with more than 6 decimal places can't be stored. Changing that needs a migration.

## Alternatives considered

- **`NUMERIC` money everywhere.** Exact, but it invites rounding at arbitrary points
  ("just store 12.3456 dollars"). Integer cents make the rounding point explicit in code.
- **Round per event.** It accumulates bias and makes totals depend on how events are batched.
  Rejected.
- **Banker's rounding.** Reasonable, but harder to explain (see above). It's a one-line
  change in `metering/rating/money.py` if needed. The rounding tests would flag the change.
