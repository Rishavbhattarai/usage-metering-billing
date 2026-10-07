"""Month-end invoicing, late-event adjustments and re-rating.

An invoice is a pure function of
  (raw events, the period's cutoff, the previous period's cutoff, price plans),
so it can be recomputed at any time from the append-only event log (re-rating), and
replaying the log gives byte-identical invoices (same content hash).

Closing period P at cutoff T_P (ADR 0003):
  * usage lines: events that occurred in P and were received at or before T_P
  * adjustment lines: events that occurred before P and were received in (T_prev, T_P],
    where T_prev is the cutoff of the previous period. They are "late": their period was
    already invoiced. For each (customer, meter, late period Q):
        adjustment = charge(Q, billed_qty + late_qty) - charge(Q, billed_qty)
    where billed_qty = Q's events received at or before T_prev. This prices late usage at
    the marginal rate of Q's plan (tiers and minimum commit included), never twice.
Periods close strictly in order, one invoice per (customer, period).
"""

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from metering.models import BillingPeriod, InvoiceLineRow, InvoiceRow
from metering.periods import next_period, period_end, period_of, period_start
from metering.pricing import PricingError, plans_for_period
from metering.rating import PricePlan, rate_with_minimum

SIX_DP = Decimal("0.000001")


class CloseError(Exception):
    """The period can't be closed (out of order, not over yet, ...)."""


@dataclass(frozen=True, slots=True)
class Line:
    kind: str  # usage | minimum | adjustment
    meter: str
    usage_period: str
    description: str
    quantity: Decimal
    unit_price: Decimal | None
    amount_cents: int


@dataclass(frozen=True, slots=True)
class Invoice:
    id: str
    customer_id: str
    period: str
    lines: tuple[Line, ...]

    @property
    def total_cents(self) -> int:
        return sum((line.amount_cents for line in self.lines), 0)

    def canonical(self) -> dict[str, Any]:
        """The invoice document. Its JSON (sorted keys, fixed decimal places) is what the
        content hash covers."""
        return {
            "id": self.id,
            "customer_id": self.customer_id,
            "period": self.period,
            "currency": "USD",
            "total_cents": self.total_cents,
            "lines": [
                {
                    "line_no": i,
                    "kind": line.kind,
                    "meter": line.meter,
                    "usage_period": line.usage_period,
                    "description": line.description,
                    "quantity": _q(line.quantity),
                    "unit_price": None if line.unit_price is None else _q(line.unit_price),
                    "amount_cents": line.amount_cents,
                }
                for i, line in enumerate(self.lines, start=1)
            ],
        }

    def canonical_json(self) -> bytes:
        return json.dumps(self.canonical(), sort_keys=True, separators=(",", ":")).encode()

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json()).hexdigest()


def _q(d: Decimal) -> str:
    return str(d.quantize(SIX_DP))


def invoice_id(customer_id: str, period: str) -> str:
    return f"inv_{period}_{customer_id}"


def charge(plan: PricePlan, quantity: Decimal) -> int:
    """Total cents for a period's usage of one meter. No usage, no charge (and no minimum)."""
    return 0 if quantity == 0 else rate_with_minimum(plan, quantity).total_cents


def _plan(plans: dict[str, PricePlan], meter: str, period: str) -> PricePlan:
    try:
        return plans[meter]
    except KeyError:
        raise PricingError(f"no price plan for meter {meter!r} in {period}") from None


async def compute_invoices(
    session: AsyncSession, period: str, cutoff: datetime, prev_cutoff: datetime | None
) -> list[Invoice]:
    """Compute (don't store) the invoices for `period`. Pure given the DB contents."""
    p_start = period_start(period)
    usage_rows = (
        await session.execute(
            text(
                """
                SELECT customer_id, meter, sum(quantity) AS qty
                FROM usage_events
                WHERE occurred_at >= :start AND occurred_at < :end AND received_at <= :cutoff
                GROUP BY customer_id, meter
                """
            ),
            {"start": p_start, "end": period_end(period), "cutoff": cutoff},
        )
    ).all()

    late_where = "occurred_at < :start AND received_at <= :cutoff"
    params: dict[str, Any] = {"start": p_start, "cutoff": cutoff}
    if prev_cutoff is not None:
        late_where += " AND received_at > :prev"
        params["prev"] = prev_cutoff
    billed_expr = "0::numeric"
    if prev_cutoff is not None:
        billed_expr = """COALESCE((
            SELECT sum(e.quantity) FROM usage_events e
            WHERE e.customer_id = l.customer_id AND e.meter = l.meter
              AND e.occurred_at >= l.q_start AND e.occurred_at < l.q_start + interval '1 month'
              AND e.received_at <= :prev), 0)"""
    late_rows = (
        await session.execute(
            text(
                f"""
                WITH late AS (
                  SELECT customer_id, meter,
                         date_trunc('month', occurred_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                           AS q_start,
                         sum(quantity) AS late_qty
                  FROM usage_events WHERE {late_where}
                  GROUP BY 1, 2, 3
                )
                SELECT l.customer_id, l.meter, l.q_start, l.late_qty, {billed_expr} AS billed_qty
                FROM late l
                """
            ),
            params,
        )
    ).all()

    plans_cache: dict[str, dict[str, PricePlan]] = {period: await plans_for_period(session, period)}

    async def plans_for(q: str) -> dict[str, PricePlan]:
        if q not in plans_cache:
            plans_cache[q] = await plans_for_period(session, q)
        return plans_cache[q]

    lines: dict[str, list[Line]] = defaultdict(list)
    for customer_id, meter, qty in sorted(usage_rows):
        plan = _plan(plans_cache[period], meter, period)
        for item in rate_with_minimum(plan, qty).lines:
            kind = "minimum" if item.description == "minimum commit shortfall" else "usage"
            lines[customer_id].append(
                Line(
                    kind,
                    meter,
                    period,
                    f"{meter}: {item.description}",
                    item.quantity,
                    item.unit_price,
                    item.amount_cents,
                )
            )

    adjustments: dict[str, list[Line]] = defaultdict(list)
    for customer_id, meter, q_start, late_qty, billed_qty in sorted(
        late_rows, key=lambda r: (r.customer_id, r.q_start, r.meter)
    ):
        q = period_of(q_start)
        plan = _plan(await plans_for(q), meter, q)
        amount = charge(plan, billed_qty + late_qty) - charge(plan, billed_qty)
        adjustments[customer_id].append(
            Line("adjustment", meter, q, f"{meter}: late usage for {q}", late_qty, None, amount)
        )

    return [
        Invoice(invoice_id(c, period), c, period, tuple(lines[c] + adjustments[c]))
        for c in sorted(set(lines) | set(adjustments))
    ]


@dataclass(frozen=True, slots=True)
class CloseResult:
    period: str
    closed_at: datetime
    already_closed: bool
    invoices: int
    total_cents: int
    adjustment_lines: int


async def _cutoffs(session: AsyncSession, period: str) -> tuple[datetime, datetime | None] | None:
    row = await session.get(BillingPeriod, period)
    if row is None:
        return None
    prev = (
        await session.execute(
            select(BillingPeriod.closed_at)
            .where(BillingPeriod.period < period)
            .order_by(BillingPeriod.period.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row.closed_at, prev


async def close_period(
    session: AsyncSession,
    period: str,
    *,
    cutoff_lag: timedelta = timedelta(seconds=60),
    closed_at: datetime | None = None,
) -> CloseResult:
    """Close `period` and write its invoices, atomically. Idempotent: closing a closed
    period returns the existing result. Caller owns the transaction.

    The cutoff is `now() - cutoff_lag` so that ingest transactions still in flight when the
    close starts (their received_at is their start time) land after the cutoff and are
    billed as late events next period instead of being missed. `closed_at` overrides the
    cutoff (used by replay).
    """
    period_start(period)  # validates format
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtext('close_period'))"))

    existing = await session.get(BillingPeriod, period)
    if existing is not None:
        rows = (
            (
                await session.execute(
                    select(InvoiceRow.total_cents).where(InvoiceRow.period == period)
                )
            )
            .scalars()
            .all()
        )
        n_adj = (
            await session.execute(
                text(
                    "SELECT count(*) FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id "
                    "WHERE i.period = :p AND l.kind = 'adjustment'"
                ),
                {"p": period},
            )
        ).scalar_one()
        return CloseResult(period, existing.closed_at, True, len(rows), sum(rows), int(n_adj))

    latest = (
        await session.execute(select(BillingPeriod).order_by(BillingPeriod.period.desc()).limit(1))
    ).scalar_one_or_none()
    if latest is not None and period != next_period(latest.period):
        raise CloseError(
            f"periods close in order: last closed is {latest.period}, "
            f"so the next one to close is {next_period(latest.period)}, not {period}"
        )

    db_now: datetime = (await session.execute(text("SELECT now()"))).scalar_one()
    cutoff = closed_at or db_now - cutoff_lag
    if cutoff < period_end(period):
        raise CloseError(f"{period} isn't over yet (cutoff {cutoff.isoformat()})")
    prev_cutoff = latest.closed_at if latest is not None else None
    if prev_cutoff is not None and cutoff <= prev_cutoff:
        raise CloseError("cutoff must be after the previous period's cutoff")

    invoices = await compute_invoices(session, period, cutoff, prev_cutoff)
    session.add(BillingPeriod(period=period, closed_at=cutoff))
    await session.flush()
    await _store(session, invoices)
    return CloseResult(
        period,
        cutoff,
        False,
        len(invoices),
        sum(i.total_cents for i in invoices),
        sum(1 for i in invoices for line in i.lines if line.kind == "adjustment"),
    )


async def _store(session: AsyncSession, invoices: list[Invoice]) -> None:
    for inv in invoices:
        inserted = (
            await session.execute(
                pg_insert(InvoiceRow)
                .values(
                    id=inv.id,
                    customer_id=inv.customer_id,
                    period=inv.period,
                    status="finalized",
                    total_cents=inv.total_cents,
                    content_hash=inv.content_hash,
                )
                .on_conflict_do_nothing(constraint="uq_invoices_customer_period")
                .returning(InvoiceRow.id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            continue
        if inv.lines:
            await session.execute(
                pg_insert(InvoiceLineRow),
                [
                    {
                        "invoice_id": inv.id,
                        "line_no": i,
                        "kind": line.kind,
                        "meter": line.meter,
                        "usage_period": line.usage_period,
                        "description": line.description,
                        "quantity": line.quantity,
                        "unit_price": line.unit_price,
                        "amount_cents": line.amount_cents,
                    }
                    for i, line in enumerate(inv.lines, start=1)
                ],
            )


async def load_invoice(session: AsyncSession, inv_id: str) -> Invoice | None:
    row = await session.get(InvoiceRow, inv_id)
    if row is None:
        return None
    lines = (
        await session.scalars(
            select(InvoiceLineRow)
            .where(InvoiceLineRow.invoice_id == inv_id)
            .order_by(InvoiceLineRow.line_no)
        )
    ).all()
    return Invoice(
        row.id,
        row.customer_id,
        row.period,
        tuple(
            Line(
                ln.kind,
                ln.meter,
                ln.usage_period,
                ln.description,
                ln.quantity,
                ln.unit_price,
                int(ln.amount_cents),
            )
            for ln in lines
        ),
    )


@dataclass
class InvoiceDiff:
    invoice_id: str
    customer_id: str
    period: str
    stored_hash: str | None
    recomputed_hash: str | None
    stored_total_cents: int
    recomputed_total_cents: int
    changed_lines: list[dict[str, Any]] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        return self.stored_hash == self.recomputed_hash

    @property
    def delta_cents(self) -> int:
        return self.recomputed_total_cents - self.stored_total_cents

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {"identical": self.identical, "delta_cents": self.delta_cents}


def diff_invoices(stored: Invoice | None, recomputed: Invoice | None) -> InvoiceDiff:
    base = stored or recomputed
    assert base is not None
    old = stored.canonical()["lines"] if stored else []
    new = recomputed.canonical()["lines"] if recomputed else []

    def key(line: dict[str, Any]) -> tuple[str, str, str, str]:
        return (line["kind"], line["meter"], line["usage_period"], line["description"])

    old_by, new_by = {key(x): x for x in old}, {key(x): x for x in new}
    changed = []
    for k in sorted(set(old_by) | set(new_by)):
        a, b = old_by.get(k), new_by.get(k)
        if (
            a is None
            or b is None
            or a["amount_cents"] != b["amount_cents"]
            or a["quantity"] != b["quantity"]
        ):
            changed.append(
                {
                    "kind": k[0],
                    "meter": k[1],
                    "usage_period": k[2],
                    "description": k[3],
                    "stored_quantity": a and a["quantity"],
                    "recomputed_quantity": b and b["quantity"],
                    "stored_cents": a and a["amount_cents"],
                    "recomputed_cents": b and b["amount_cents"],
                }
            )
    return InvoiceDiff(
        base.id,
        base.customer_id,
        base.period,
        stored.content_hash if stored else None,
        recomputed.content_hash if recomputed else None,
        stored.total_cents if stored else 0,
        recomputed.total_cents if recomputed else 0,
        changed,
    )


async def rerate_period(session: AsyncSession, period: str) -> list[InvoiceDiff]:
    """Recompute every invoice of a closed period from raw events (with the stored cutoffs and
    the plans in effect now) and diff against what was stored. Read-only."""
    cut = await _cutoffs(session, period)
    if cut is None:
        raise CloseError(f"{period} isn't closed")
    recomputed = {i.id: i for i in await compute_invoices(session, period, *cut)}
    stored_ids = (
        (await session.execute(select(InvoiceRow.id).where(InvoiceRow.period == period)))
        .scalars()
        .all()
    )
    diffs = []
    for inv_id in sorted(set(stored_ids) | set(recomputed)):
        diffs.append(diff_invoices(await load_invoice(session, inv_id), recomputed.get(inv_id)))
    return diffs


async def rerate_invoice(session: AsyncSession, inv_id: str) -> InvoiceDiff | None:
    stored = await load_invoice(session, inv_id)
    if stored is None:
        return None
    cut = await _cutoffs(session, stored.period)
    assert cut is not None
    recomputed = {i.id: i for i in await compute_invoices(session, stored.period, *cut)}
    return diff_invoices(stored, recomputed.get(inv_id))
