"""price plans, billing periods, invoices, Salesforce sync log, reconciliation runs

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)
QUANTITY = sa.Numeric(20, 6)
PERIOD_CHECK = r"~ '^\d{4}-(0[1-9]|1[0-2])$'"


def upgrade() -> None:
    # Close windows and dirty-hour detection both scan by received_at; the close also
    # scans by occurred_at.
    op.create_index("ix_usage_events_received_at", "usage_events", ["received_at"])
    op.create_index("ix_usage_events_occurred_at", "usage_events", ["occurred_at"])

    op.create_table(
        "aggregation_state",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("watermark", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
    )
    # Daily usage is a view over the hourly rollup: at most 24 rows per cell, always
    # consistent with usage_hourly, nothing extra to keep in sync.
    op.execute(
        """
        CREATE VIEW usage_daily AS
        SELECT customer_id, meter, (hour AT TIME ZONE 'UTC')::date AS day,
               sum(quantity) AS quantity, max(updated_at) AS updated_at
        FROM usage_hourly
        GROUP BY customer_id, meter, (hour AT TIME ZONE 'UTC')::date
        """
    )

    op.create_table(
        "price_plans",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("meter", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("unit_price", sa.Numeric(20, 6)),
        sa.Column("tiers", JSONB),
        sa.Column("min_commit_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("effective_from", sa.Date, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("meter", "effective_from", name="uq_price_plans_meter_from"),
        sa.CheckConstraint("model IN ('flat', 'graduated')", name="price_plans_model"),
        sa.CheckConstraint(
            "(model = 'flat' AND unit_price IS NOT NULL AND tiers IS NULL)"
            " OR (model = 'graduated' AND tiers IS NOT NULL AND unit_price IS NULL)",
            name="price_plans_model_fields",
        ),
        sa.CheckConstraint("min_commit_cents >= 0", name="price_plans_min_commit_nonneg"),
    )

    op.create_table(
        "billing_periods",
        sa.Column("period", sa.Text, primary_key=True),
        # Cutoff: events with received_at <= closed_at are billed on this period's invoices.
        sa.Column("closed_at", TS, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(f"period {PERIOD_CHECK}", name="billing_periods_format"),
    )

    op.create_table(
        "invoices",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("customer_id", sa.Text, nullable=False),
        sa.Column("period", sa.Text, sa.ForeignKey("billing_periods.period"), nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="finalized"),
        sa.Column("total_cents", sa.BigInteger, nullable=False),
        sa.Column("content_hash", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("customer_id", "period", name="uq_invoices_customer_period"),
        sa.CheckConstraint("total_cents >= 0", name="invoices_total_nonneg"),
    )
    op.create_table(
        "invoice_lines",
        sa.Column(
            "invoice_id",
            sa.Text,
            sa.ForeignKey("invoices.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("line_no", sa.Integer, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("meter", sa.Text, nullable=False),
        # The period the usage belongs to: the invoice period for usage/minimum lines, an
        # earlier period for adjustment lines (late events).
        sa.Column("usage_period", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("quantity", QUANTITY, nullable=False),
        sa.Column("unit_price", sa.Numeric(20, 6)),
        sa.Column("amount_cents", sa.BigInteger, nullable=False),
        sa.CheckConstraint("kind IN ('usage', 'minimum', 'adjustment')", name="invoice_lines_kind"),
        sa.CheckConstraint("amount_cents >= 0", name="invoice_lines_amount_nonneg"),
    )

    op.create_table(
        "sf_sync_log",
        sa.Column("entity", sa.Text, primary_key=True),
        sa.Column("external_id", sa.Text, primary_key=True),
        sa.Column("payload_hash", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("sf_id", sa.Text),
        sa.Column("error", sa.Text),
        sa.Column("synced_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('ok', 'failed')", name="sf_sync_log_status"),
    )

    op.create_table(
        "reconciliation_runs",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("period", sa.Text, nullable=False),
        sa.Column("ran_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("checked", sa.Integer, nullable=False),
        sa.Column("missing", sa.Integer, nullable=False),
        sa.Column("mismatched", sa.Integer, nullable=False),
        sa.Column("ledger_total_cents", sa.BigInteger, nullable=False),
        sa.Column("salesforce_total_cents", sa.BigInteger, nullable=False),
        sa.Column("details", JSONB, nullable=False),
    )
    op.create_index("ix_reconciliation_runs_period", "reconciliation_runs", ["period", "id"])


def downgrade() -> None:
    op.drop_table("reconciliation_runs")
    op.drop_table("sf_sync_log")
    op.drop_table("invoice_lines")
    op.drop_table("invoices")
    op.drop_table("billing_periods")
    op.drop_table("price_plans")
    op.execute("DROP VIEW IF EXISTS usage_daily")
    op.drop_table("aggregation_state")
    op.drop_index("ix_usage_events_occurred_at", table_name="usage_events")
    op.drop_index("ix_usage_events_received_at", table_name="usage_events")
