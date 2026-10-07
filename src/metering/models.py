"""ORM table definitions. Alembic migrations in migrations/versions are the source of truth
for the schema; these models mirror them for querying."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Quantities and unit prices: NUMERIC(20, 6). Money amounts: BIGINT cents. Never float.
QUANTITY = Numeric(20, 6)


class Base(DeclarativeBase):
    pass


class UsageEvent(Base):
    """Append-only raw usage. A DB trigger rejects UPDATE and DELETE."""

    __tablename__ = "usage_events"

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    meter: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UsageHourly(Base):
    """Hourly rollup of usage_events. Populated by the aggregator (not implemented yet)."""

    __tablename__ = "usage_hourly"

    customer_id: Mapped[str] = mapped_column(Text, primary_key=True)
    meter: Mapped[str] = mapped_column(Text, primary_key=True)
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
