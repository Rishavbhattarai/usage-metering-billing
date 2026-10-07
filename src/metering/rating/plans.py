"""Price plan definitions (immutable, validated on construction)."""

from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise

from metering.rating.money import Cents


def _check_amount(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal, got {type(value).__name__}")
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a finite, non-negative Decimal, got {value}")


@dataclass(frozen=True, slots=True)
class FlatPricing:
    """quantity x unit_price."""

    unit_price: Decimal

    def __post_init__(self) -> None:
        _check_amount("unit_price", self.unit_price)


@dataclass(frozen=True, slots=True)
class Tier:
    """Covers quantities in (previous tier's up_to, up_to]. up_to=None means unbounded."""

    up_to: Decimal | None
    unit_price: Decimal

    def __post_init__(self) -> None:
        _check_amount("unit_price", self.unit_price)
        if self.up_to is not None:
            _check_amount("up_to", self.up_to)
            if self.up_to == 0:
                raise ValueError("up_to must be > 0")


@dataclass(frozen=True, slots=True)
class GraduatedPricing:
    """Graduated (a.k.a. tiered) pricing: each unit is charged at the price of the tier it
    falls in. E.g. first 1000 @ $0.10, next 9000 @ $0.08, rest @ $0.05."""

    tiers: tuple[Tier, ...]

    def __post_init__(self) -> None:
        if not self.tiers:
            raise ValueError("at least one tier is required")
        if self.tiers[-1].up_to is not None:
            raise ValueError("the last tier must be unbounded (up_to=None)")
        bounds = [t.up_to for t in self.tiers[:-1]]
        if any(b is None for b in bounds):
            raise ValueError("only the last tier may be unbounded")
        if any(a >= b for a, b in pairwise(bounds)):  # type: ignore[operator]
            raise ValueError("tier up_to values must be strictly increasing")


Pricing = FlatPricing | GraduatedPricing


@dataclass(frozen=True, slots=True)
class PricePlan:
    """Pricing for one meter, plus an optional minimum commit for the period."""

    meter: str
    pricing: Pricing
    min_commit_cents: Cents = 0

    def __post_init__(self) -> None:
        if not isinstance(self.min_commit_cents, int) or self.min_commit_cents < 0:
            raise ValueError("min_commit_cents must be a non-negative int")
