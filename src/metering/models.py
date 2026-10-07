"""ORM table definitions. Alembic migrations in migrations/versions are the source of truth
for the schema; these models mirror them for querying."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    Numeric,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Quantities and unit prices: NUMERIC(20, 6). Money amounts: BIGINT cents. Never float.
QUANTITY = Numeric(20, 6)
TS = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


class UsageEvent(Base):
    """Append-only raw usage. A DB trigger rejects UPDATE and DELETE."""

    __tablename__ = "usage_events"

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    meter: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    received_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class UsageHourly(Base):
    """Hourly rollup of usage_events, maintained by metering.aggregation."""

    __tablename__ = "usage_hourly"

    customer_id: Mapped[str] = mapped_column(Text, primary_key=True)
    meter: Mapped[str] = mapped_column(Text, primary_key=True)
    hour: Mapped[datetime] = mapped_column(TS, primary_key=True)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class PricePlanRow(Base):
    __tablename__ = "price_plans"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    meter: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    tiers: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB(none_as_null=True))
    min_commit_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)


class BillingPeriod(Base):
    __tablename__ = "billing_periods"

    period: Mapped[str] = mapped_column(Text, primary_key=True)
    closed_at: Mapped[datetime] = mapped_column(TS, nullable=False)


class InvoiceRow(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    period: Mapped[str] = mapped_column(ForeignKey("billing_periods.period"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="finalized")
    total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class InvoiceLineRow(Base):
    __tablename__ = "invoice_lines"

    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"), primary_key=True)
    line_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    meter: Mapped[str] = mapped_column(Text, nullable=False)
    usage_period: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)


class SyncLogRow(Base):
    __tablename__ = "sf_sync_log"

    entity: Mapped[str] = mapped_column(Text, primary_key=True)
    external_id: Mapped[str] = mapped_column(Text, primary_key=True)
    payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    sf_id: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    period: Mapped[str] = mapped_column(Text, nullable=False)
    ran_at: Mapped[datetime] = mapped_column(TS, nullable=False, server_default=func.now())
    checked: Mapped[int] = mapped_column(Integer, nullable=False)
    missing: Mapped[int] = mapped_column(Integer, nullable=False)
    mismatched: Mapped[int] = mapped_column(Integer, nullable=False)
    ledger_total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    salesforce_total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
