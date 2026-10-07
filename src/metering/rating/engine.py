"""Rating engine: pure functions from (pricing, quantity) to invoice lines.

No I/O, no clocks, no globals (the decimal contexts are local), so the same inputs always
give byte-identical output. That is what makes re-rating and replay checks possible.
"""

from dataclasses import dataclass
from decimal import Decimal

from metering.rating.money import Cents, exact_mul, exact_sub, to_cents
from metering.rating.plans import FlatPricing, GraduatedPricing, PricePlan, Pricing

_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class LineItem:
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount_cents: Cents


@dataclass(frozen=True, slots=True)
class RatedUsage:
    lines: tuple[LineItem, ...]

    @property
    def total_cents(self) -> Cents:
        return sum((line.amount_cents for line in self.lines), 0)


def _check_quantity(quantity: Decimal) -> None:
    if not isinstance(quantity, Decimal):
        raise TypeError(f"quantity must be Decimal, got {type(quantity).__name__}")
    if not quantity.is_finite() or quantity < 0:
        raise ValueError(f"quantity must be a finite, non-negative Decimal, got {quantity}")


def _line(description: str, quantity: Decimal, unit_price: Decimal) -> LineItem:
    # The single rounding point: exact product, then half-up to cents, once per line.
    return LineItem(description, quantity, unit_price, to_cents(exact_mul(quantity, unit_price)))


def rate_flat(pricing: FlatPricing, quantity: Decimal) -> RatedUsage:
    _check_quantity(quantity)
    if quantity == 0:
        return RatedUsage(lines=())
    return RatedUsage(lines=(_line("flat", quantity, pricing.unit_price),))


def tier_quantities(pricing: GraduatedPricing, quantity: Decimal) -> tuple[Decimal, ...]:
    """Split `quantity` across tiers. The parts always sum exactly to `quantity`."""
    _check_quantity(quantity)
    parts: list[Decimal] = []
    lower = _ZERO
    for tier in pricing.tiers:
        if tier.up_to is None:
            part = max(_ZERO, exact_sub(quantity, lower))
        else:
            part = max(_ZERO, exact_sub(min(quantity, tier.up_to), lower))
            lower = tier.up_to
        parts.append(part)
    return tuple(parts)


def rate_graduated(pricing: GraduatedPricing, quantity: Decimal) -> RatedUsage:
    lines: list[LineItem] = []
    lower = _ZERO
    for tier, part in zip(pricing.tiers, tier_quantities(pricing, quantity), strict=True):
        upper = "inf" if tier.up_to is None else str(tier.up_to)
        if part > 0:
            lines.append(_line(f"tier ({lower}, {upper}]", part, tier.unit_price))
        if tier.up_to is not None:
            lower = tier.up_to
    return RatedUsage(lines=tuple(lines))


def rate(pricing: Pricing, quantity: Decimal) -> RatedUsage:
    match pricing:
        case FlatPricing():
            return rate_flat(pricing, quantity)
        case GraduatedPricing():
            return rate_graduated(pricing, quantity)


def minimum_commit_shortfall(rated_total_cents: Cents, min_commit_cents: Cents) -> Cents:
    """Amount needed to top a period's usage charges up to the minimum commit (>= 0)."""
    if min_commit_cents < 0 or rated_total_cents < 0:
        raise ValueError("amounts must be non-negative")
    return max(0, min_commit_cents - rated_total_cents)


def rate_with_minimum(plan: PricePlan, quantity: Decimal) -> RatedUsage:
    """Rate usage and, if below the minimum commit, add a shortfall line so that
    total == max(usage charges, min_commit_cents)."""
    rated = rate(plan.pricing, quantity)
    shortfall = minimum_commit_shortfall(rated.total_cents, plan.min_commit_cents)
    if shortfall == 0:
        return rated
    line = LineItem(
        "minimum commit shortfall", Decimal(1), Decimal(shortfall).scaleb(-2), shortfall
    )
    return RatedUsage(lines=(*rated.lines, line))
