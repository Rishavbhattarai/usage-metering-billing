"""Property tests for the rating engine (Hypothesis)."""

from decimal import Decimal as D
from decimal import localcontext

from hypothesis import assume, given
from hypothesis import strategies as st

from metering.rating import (
    FlatPricing,
    GraduatedPricing,
    PricePlan,
    Tier,
    rate,
    rate_with_minimum,
    tier_quantities,
)

# Ranges match the DB: NUMERIC(20, 6) quantities and prices.
quantities = st.decimals(min_value=0, max_value=D(10) ** 12, places=6)
prices = st.decimals(min_value=0, max_value=D(1000), places=6)


@st.composite
def graduated(draw: st.DrawFn) -> GraduatedPricing:
    bounds = draw(
        st.lists(
            st.decimals(min_value=D("0.000001"), max_value=D(10) ** 9, places=6),
            max_size=6,
            unique=True,
        )
    )
    ups: list[D | None] = [*sorted(bounds), None]
    return GraduatedPricing(tuple(Tier(up, draw(prices)) for up in ups))


pricings = st.one_of(prices.map(FlatPricing), graduated())


@given(pricings, quantities)
def test_total_is_never_negative(pricing: FlatPricing | GraduatedPricing, q: D) -> None:
    assert rate(pricing, q).total_cents >= 0


@given(pricings, quantities, quantities)
def test_total_is_monotonic_in_quantity(
    pricing: FlatPricing | GraduatedPricing, a: D, b: D
) -> None:
    lo, hi = sorted((a, b))
    assert rate(pricing, lo).total_cents <= rate(pricing, hi).total_cents


@given(prices, quantities)
def test_flat_equals_single_tier_graduated(price: D, q: D) -> None:
    flat = rate(FlatPricing(price), q)
    tiered = rate(GraduatedPricing((Tier(None, price),)), q)
    assert flat.total_cents == tiered.total_cents
    assert [line.amount_cents for line in flat.lines] == [
        line.amount_cents for line in tiered.lines
    ]


@given(graduated(), quantities)
def test_tier_quantities_sum_exactly_to_quantity(pricing: GraduatedPricing, q: D) -> None:
    parts = tier_quantities(pricing, q)
    assert all(p >= 0 for p in parts)
    assert sum(parts, D(0)) == q


@given(graduated(), quantities)
def test_rounding_error_is_at_most_half_a_cent_per_line(pricing: GraduatedPricing, q: D) -> None:
    rated = rate(pricing, q)
    parts = zip(tier_quantities(pricing, q), pricing.tiers, strict=True)
    with localcontext(prec=60):  # default 28 digits would itself round
        exact_cents = sum((part * tier.unit_price * 100 for part, tier in parts), D(0))
    assert abs(D(rated.total_cents) - exact_cents) <= D("0.5") * len(rated.lines)


@given(graduated(), st.data())
def test_crossing_a_boundary_adds_next_tier_price(
    pricing: GraduatedPricing, data: st.DataObject
) -> None:
    """Just above a boundary b, the extra quantity is charged at the *next* tier's price."""
    bounded = [i for i, t in enumerate(pricing.tiers) if t.up_to is not None]
    assume(bounded)
    i = data.draw(st.sampled_from(bounded))
    b = pricing.tiers[i].up_to
    assert b is not None
    step = D(1)
    at_b = rate(pricing, b).total_cents
    nxt = pricing.tiers[i + 1]
    room = step if nxt.up_to is None else min(step, nxt.up_to - b)
    above = rate(pricing, b + room).total_cents
    # Tiers up to i are full and identical in both; only the new line differs, and it is
    # rounded once, so the difference is within half a cent of the exact charge.
    with localcontext(prec=60):
        assert abs(D(above - at_b) - room * nxt.unit_price * 100) <= D("0.5")


@given(pricings, quantities, st.integers(min_value=0, max_value=10**9))
def test_minimum_commit_is_a_floor(
    pricing: FlatPricing | GraduatedPricing, q: D, minimum: int
) -> None:
    usage = rate(pricing, q).total_cents
    total = rate_with_minimum(PricePlan("m", pricing, minimum), q).total_cents
    assert total == max(usage, minimum)


@given(pricings, quantities)
def test_rating_is_deterministic(pricing: FlatPricing | GraduatedPricing, q: D) -> None:
    assert rate(pricing, q) == rate(pricing, D(str(q)))
