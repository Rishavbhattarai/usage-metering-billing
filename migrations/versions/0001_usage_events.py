"""usage_events (append-only) and usage_hourly

Revision ID: 0001
Revises:
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QUANTITY = sa.Numeric(20, 6)


def upgrade() -> None:
    op.create_table(
        "usage_events",
        sa.Column("event_id", sa.Text, primary_key=True),
        sa.Column("customer_id", sa.Text, nullable=False),
        sa.Column("meter", sa.Text, nullable=False),
        sa.Column("quantity", QUANTITY, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("quantity >= 0", name="usage_events_quantity_nonneg"),
    )
    # Aggregation and invoicing scan by customer/meter over a time range.
    op.create_index(
        "ix_usage_events_customer_meter_occurred",
        "usage_events",
        ["customer_id", "meter", "occurred_at"],
    )

    # Append-only: raw events are never edited or deleted. (TRUNCATE is still allowed, for
    # tests and resets; it does not fire row-level triggers.)
    op.execute(
        """
        CREATE FUNCTION usage_events_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'usage_events is append-only (% rejected)', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER usage_events_append_only
        BEFORE UPDATE OR DELETE ON usage_events
        FOR EACH ROW EXECUTE FUNCTION usage_events_append_only();
        """
    )

    op.create_table(
        "usage_hourly",
        sa.Column("customer_id", sa.Text, primary_key=True),
        sa.Column("meter", sa.Text, primary_key=True),
        sa.Column("hour", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("quantity", QUANTITY, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("quantity >= 0", name="usage_hourly_quantity_nonneg"),
        # Hour buckets are UTC-aligned regardless of the session TimeZone setting.
        sa.CheckConstraint(
            "hour AT TIME ZONE 'UTC' = date_trunc('hour', hour AT TIME ZONE 'UTC')",
            name="usage_hourly_hour_aligned_utc",
        ),
    )


def downgrade() -> None:
    op.drop_table("usage_hourly")
    op.execute("DROP TRIGGER IF EXISTS usage_events_append_only ON usage_events")
    op.execute("DROP FUNCTION IF EXISTS usage_events_append_only()")
    op.drop_index("ix_usage_events_customer_meter_occurred", table_name="usage_events")
    op.drop_table("usage_events")
