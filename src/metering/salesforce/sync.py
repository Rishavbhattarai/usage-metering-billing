"""Ledger -> Salesforce sync and reconciliation (ADRs 0004, 0005).

Only aggregates go to Salesforce: one Account per customer, one Usage_Summary__c per
(customer, meter, usage period) and one Invoice__c per invoice. Raw events never do
(Developer Edition API and storage limits).

Idempotency: every record is upserted by its External ID, so re-running a sync (a jobq
retry, a manual re-run) updates in place. sf_sync_log keeps the hash of the last payload
that synced OK per record, so unchanged records aren't re-sent; that saves API calls.
"""

import asyncio
import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from metering.models import ReconciliationRun, SyncLogRow
from metering.retry import retry_async
from metering.salesforce.client import (
    ACCOUNT,
    ACCOUNT_EXT_ID,
    SalesforceClient,
    SalesforceError,
    UpsertResult,
)

USAGE = "Usage_Summary__c"
USAGE_EXT_ID = "External_Key__c"
INVOICE = "Invoice__c"
INVOICE_EXT_ID = "Invoice_Ext_Id__c"
SIX_DP = Decimal("0.000001")

# Sync order matters: children look up their Account by External ID.
ENTITIES: tuple[tuple[str, str], ...] = (
    (ACCOUNT, ACCOUNT_EXT_ID),
    (USAGE, USAGE_EXT_ID),
    (INVOICE, INVOICE_EXT_ID),
)
COMPARE_FIELDS: dict[str, tuple[str, ...]] = {
    ACCOUNT: ("Name",),
    USAGE: ("Account__r.Billing_Customer_Id__c", "Meter__c", "Period__c", "Quantity__c"),
    INVOICE: ("Account__r.Billing_Customer_Id__c", "Period__c", "Total__c", "Status__c"),
}


class SyncFailedError(Exception):
    """Some records didn't sync. Raised so the job is retried (and dead-lettered by jobq
    once its attempts run out)."""


def _account(c: str) -> dict[str, Any]:
    return {ACCOUNT_EXT_ID: c, "Name": c}


def _ref(c: str) -> dict[str, str]:
    return {ACCOUNT_EXT_ID: c}


def cents_to_currency(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100}.{abs(cents) % 100:02d}"


async def ledger_records(session: AsyncSession, period: str) -> dict[str, list[dict[str, Any]]]:
    """What Salesforce should hold for the invoices of `period`, built from the ledger.

    Usage summaries cover the invoice period and any earlier period that this period's
    invoices adjust. Each summary's quantity is everything billed for that usage period on
    any invoice so far, so re-syncing an old period never rolls back a later adjustment.
    """
    invoices = (
        await session.execute(
            text(
                "SELECT id, customer_id, period, total_cents, status FROM invoices "
                "WHERE period = :p ORDER BY id"
            ),
            {"p": period},
        )
    ).all()
    usage = (
        await session.execute(
            text(
                """
                WITH keys AS (
                  SELECT DISTINCT i.customer_id, l.meter, l.usage_period
                  FROM invoice_lines l JOIN invoices i ON i.id = l.invoice_id
                  WHERE i.period = :p AND l.kind IN ('usage', 'adjustment')
                )
                SELECT k.customer_id, k.meter, k.usage_period, sum(l.quantity) AS qty
                FROM keys k
                JOIN invoices i ON i.customer_id = k.customer_id
                JOIN invoice_lines l ON l.invoice_id = i.id AND l.meter = k.meter
                     AND l.usage_period = k.usage_period AND l.kind IN ('usage', 'adjustment')
                GROUP BY k.customer_id, k.meter, k.usage_period
                ORDER BY 1, 2, 3
                """
            ),
            {"p": period},
        )
    ).all()
    customers = sorted({row.customer_id for row in invoices})
    return {
        ACCOUNT: [_account(c) for c in customers],
        USAGE: [
            {
                USAGE_EXT_ID: f"{r.customer_id}:{r.meter}:{r.usage_period}",
                "Account__r": _ref(r.customer_id),
                "Meter__c": r.meter,
                "Period__c": r.usage_period,
                "Quantity__c": str(Decimal(r.qty).quantize(SIX_DP)),
            }
            for r in usage
        ],
        INVOICE: [
            {
                INVOICE_EXT_ID: r.id,
                "Account__r": _ref(r.customer_id),
                "Period__c": r.period,
                "Total__c": cents_to_currency(int(r.total_cents)),
                "Status__c": r.status.capitalize(),
            }
            for r in invoices
        ],
    }


def payload_hash(record: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()


@dataclass
class SyncResult:
    period: str
    sent: dict[str, int] = field(default_factory=dict)
    skipped_unchanged: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def sync_period(
    sessions: async_sessionmaker[AsyncSession],
    sf: SalesforceClient,
    period: str,
    *,
    force: bool = False,
    attempts: int = 3,
    backoff_base: float = 0.5,
) -> SyncResult:
    """Push the period's ledger records to Salesforce. Each API call is retried with
    backoff on call-level errors; if records still fail, raises SyncFailedError after
    logging what did succeed, so a retry only re-sends what's missing."""
    async with sessions() as session:
        wanted = await ledger_records(session, period)
    result = SyncResult(period)
    for sobject, ext_field in ENTITIES:
        records = wanted[sobject]
        hashes = {r[ext_field]: payload_hash(r) for r in records}
        if not force and records:
            async with sessions() as session:
                synced = {
                    (row.external_id, row.payload_hash)
                    for row in (
                        await session.scalars(
                            select(SyncLogRow).where(
                                SyncLogRow.entity == sobject,
                                SyncLogRow.status == "ok",
                                SyncLogRow.external_id.in_(list(hashes)),
                            )
                        )
                    ).all()
                }
            todo = [r for r in records if (r[ext_field], hashes[r[ext_field]]) not in synced]
        else:
            todo = list(records)
        result.skipped_unchanged[sobject] = len(records) - len(todo)
        result.sent[sobject] = len(todo)
        if not todo:
            continue

        async def call(
            batch: Sequence[dict[str, Any]] = todo, obj: str = sobject, ext: str = ext_field
        ) -> list[UpsertResult]:
            return await asyncio.to_thread(sf.upsert, obj, ext, batch)

        try:
            outcomes = await retry_async(
                call, retry_on=(SalesforceError,), attempts=attempts, base=backoff_base
            )
        except SalesforceError as exc:
            result.failed[sobject] = len(todo)
            result.errors.append(f"{sobject}: {exc}")
            raise SyncFailedError(
                f"sync {period}: {sobject} failed after {attempts} tries: {exc}"
            ) from exc

        async with sessions() as session, session.begin():
            for out in outcomes:
                values = {
                    "entity": sobject,
                    "external_id": out.external_id,
                    "payload_hash": hashes.get(out.external_id, ""),
                    "status": "ok" if out.success else "failed",
                    "sf_id": out.sf_id,
                    "error": out.error,
                }
                await session.execute(
                    pg_insert(SyncLogRow)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["entity", "external_id"],
                        set_={**values, "synced_at": text("now()")},
                    )
                )
        failed = [o for o in outcomes if not o.success]
        result.failed[sobject] = len(failed)
        result.errors.extend(f"{sobject} {o.external_id}: {o.error}" for o in failed[:20])
    if any(result.failed.values()):
        raise SyncFailedError(f"sync {period}: {result.failed} records failed: {result.errors[:5]}")
    return result


# ---------------------------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------------------------


def _norm(field_name: str, value: Any) -> Any:
    if value is None:
        return None
    if field_name == "Quantity__c":
        return Decimal(str(value)).quantize(SIX_DP)
    if field_name == "Total__c":
        return Decimal(str(value)).quantize(Decimal("0.01"))
    return str(value)


def _expected(sobject: str, record: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for f in COMPARE_FIELDS[sobject]:
        if f.startswith("Account__r."):
            out[f] = record["Account__r"][ACCOUNT_EXT_ID]
        else:
            out[f] = record[f]
    return out


@dataclass
class DriftReport:
    period: str
    checked: int = 0
    missing: list[dict[str, str]] = field(default_factory=list)
    mismatched: list[dict[str, Any]] = field(default_factory=list)
    ledger_total_cents: int = 0
    salesforce_total_cents: int = 0

    @property
    def drift(self) -> int:
        return len(self.missing) + len(self.mismatched)

    def as_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "checked": self.checked,
            "drift": self.drift,
            "missing": self.missing[:100],
            "mismatched": self.mismatched[:100],
            "ledger_total_cents": self.ledger_total_cents,
            "salesforce_total_cents": self.salesforce_total_cents,
        }


async def reconcile_period(
    sessions: async_sessionmaker[AsyncSession],
    sf: SalesforceClient,
    period: str,
    *,
    repair: bool = False,
) -> DriftReport:
    """Compare the ledger with Salesforce for one period and store the report.

    With repair=True, drifted records are cleared from sf_sync_log so the next sync re-sends
    them (the caller then enqueues a sync)."""
    async with sessions() as session:
        wanted = await ledger_records(session, period)
    report = DriftReport(period)
    drifted: list[tuple[str, str]] = []
    for sobject, ext_field in ENTITIES:
        records = wanted[sobject]
        ids = [r[ext_field] for r in records]
        actual = await asyncio.to_thread(sf.fetch, sobject, ext_field, ids, COMPARE_FIELDS[sobject])
        for r in records:
            report.checked += 1
            ext = r[ext_field]
            if sobject == INVOICE:
                report.ledger_total_cents += int(Decimal(r["Total__c"]) * 100)
            got = actual.get(ext)
            if got is None:
                report.missing.append({"sobject": sobject, "external_id": ext})
                drifted.append((sobject, ext))
                continue
            if sobject == INVOICE and got.get("Total__c") is not None:
                report.salesforce_total_cents += int(_norm("Total__c", got["Total__c"]) * 100)
            exp = _expected(sobject, r)
            diffs = {
                f: {"ledger": str(_norm(f, exp[f])), "salesforce": str(_norm(f, got.get(f)))}
                for f in exp
                if _norm(f, exp[f]) != _norm(f, got.get(f))
            }
            if diffs:
                report.mismatched.append({"sobject": sobject, "external_id": ext, "fields": diffs})
                drifted.append((sobject, ext))

    async with sessions() as session, session.begin():
        session.add(
            ReconciliationRun(
                period=period,
                checked=report.checked,
                missing=len(report.missing),
                mismatched=len(report.mismatched),
                ledger_total_cents=report.ledger_total_cents,
                salesforce_total_cents=report.salesforce_total_cents,
                details=report.as_dict(),
            )
        )
        if repair and drifted:
            await session.execute(
                delete(SyncLogRow).where(
                    tuple_(SyncLogRow.entity, SyncLogRow.external_id).in_(drifted)
                )
            )
    return report
