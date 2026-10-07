"""Price plans in the database <-> rating-engine PricePlan objects.

A plan applies to one meter from `effective_from` onward. The plan used for a period is
the one with the latest effective_from on or before the period's first day. Changing a
price means inserting a new row, never editing an old one, so past periods can always be
re-rated with the plan they were billed with.
"""

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from metering.models import PricePlanRow
from metering.periods import period_start
from metering.rating import FlatPricing, GraduatedPricing, PricePlan, Tier


class PricingError(LookupError):
    """No usable price plan. Billing must stop rather than charge $0."""


def plain(d: Decimal) -> str:
    """Decimal as a plain string without trailing zeros or exponent: 1000.000000 -> '1000'."""
    s = format(d.normalize(), "f")
    return s


def plan_from_row(row: PricePlanRow) -> PricePlan:
    if row.model == "flat":
        assert row.unit_price is not None
        pricing: FlatPricing | GraduatedPricing = FlatPricing(Decimal(plain(row.unit_price)))
    elif row.model == "graduated":
        assert row.tiers is not None
        pricing = GraduatedPricing(
            tuple(
                Tier(
                    None if t["up_to"] is None else Decimal(str(t["up_to"])),
                    Decimal(str(t["unit_price"])),
                )
                for t in row.tiers
            )
        )
    else:
        raise PricingError(f"unknown pricing model {row.model!r}")
    return PricePlan(row.meter, pricing, int(row.min_commit_cents))


def plan_to_values(plan: PricePlan, effective_from: date) -> dict[str, Any]:
    values: dict[str, Any] = {
        "meter": plan.meter,
        "min_commit_cents": plan.min_commit_cents,
        "effective_from": effective_from,
        "unit_price": None,
        "tiers": None,
    }
    match plan.pricing:
        case FlatPricing(unit_price=price):
            values |= {"model": "flat", "unit_price": price}
        case GraduatedPricing(tiers=tiers):
            values |= {
                "model": "graduated",
                "tiers": [
                    {
                        "up_to": None if t.up_to is None else plain(t.up_to),
                        "unit_price": plain(t.unit_price),
                    }
                    for t in tiers
                ],
            }
    return values


async def plans_for_period(session: AsyncSession, period: str) -> dict[str, PricePlan]:
    """meter -> plan in effect on the first day of `period`."""
    first_day = period_start(period).date()
    stmt = select(PricePlanRow).from_statement(
        text(
            "SELECT DISTINCT ON (meter) * FROM price_plans WHERE effective_from <= :d "
            "ORDER BY meter, effective_from DESC"
        )
    )
    rows = (await session.scalars(stmt, {"d": first_day})).all()
    return {row.meter: plan_from_row(row) for row in rows}


async def upsert_plan(session: AsyncSession, plan: PricePlan, effective_from: date) -> bool:
    """Insert a plan version. Returns False if (meter, effective_from) already exists."""
    stmt = (
        pg_insert(PricePlanRow)
        .values(**plan_to_values(plan, effective_from))
        .on_conflict_do_nothing(constraint="uq_price_plans_meter_from")
        .returning(PricePlanRow.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


D = Decimal
DEFAULT_EFFECTIVE_FROM = date(2020, 1, 1)
DEFAULT_PLANS: Mapping[str, PricePlan] = {
    # Graduated: first 1,000 calls at $0.10, the next 9,000 at $0.08, the rest at $0.05.
    "api_calls": PricePlan(
        "api_calls",
        GraduatedPricing(
            (Tier(D(1000), D("0.10")), Tier(D(10000), D("0.08")), Tier(None, D("0.05")))
        ),
    ),
    # Flat $0.023 per GB-month, with a $50.00 monthly minimum commit.
    "gb_stored": PricePlan("gb_stored", FlatPricing(D("0.023")), min_commit_cents=5000),
}


async def seed_default_plans(session: AsyncSession) -> int:
    inserted = 0
    for plan in DEFAULT_PLANS.values():
        inserted += await upsert_plan(session, plan, DEFAULT_EFFECTIVE_FROM)
    return inserted
