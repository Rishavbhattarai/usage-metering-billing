"""Hand-checked rating examples, with emphasis on tier boundaries."""

from decimal import Decimal as D

import pytest

from metering.rating import (
    FlatPricing,
    GraduatedPricing,
    PricePlan,
    Tier,
    format_cents,
    minimum_commit_shortfall,
    rate,
    rate_with_minimum,
    tier_quantities,
    to_cents,
)

# First 1,000 @ $0.10, next 9,000 @ $0.08, everything above 10,000 @ $0.05.
API_CALLS = GraduatedPricing(
    (Tier(D(1000), D("0.10")), Tier(D(10000), D("0.08")), Tier(None, D("0.05")))
)


@pytest.mark.parametrize(
    ("quantity", "expected_cents"),
    [
        ("0", 0),
        ("1", 10),
        ("999", 99_90),
        ("1000", 100_00),  # exactly on the first boundary: all in tier 1
        ("1000.000001", 100_00),  # 0.000001 x $0.08 rounds to 0 cents
        ("1001", 100_08),  # first unit of tier 2
        ("9999", 100_00 + 8999 * 8),
        ("10000", 100_00 + 720_00),  # exactly on the second boundary
        ("10001", 820_00 + 5),
        ("12345.5", 820_00 + 117_28),  # 2345.5 x 0.05 = 117.275 -> half-up 117.28
    ],
)
def test_graduated_hand_checked(quantity: str, expected_cents: int) -> None:
    assert rate(API_CALLS, D(quantity)).total_cents == expected_cents


def test_boundary_is_inclusive_upper_bound() -> None:
    assert tier_quantities(API_CALLS, D(1000)) == (D(1000), D(0), D(0))
    assert tier_quantities(API_CALLS, D(1001)) == (D(1000), D(1), D(0))
    assert tier_quantities(API_CALLS, D(10000)) == (D(1000), D(9000), D(0))
    assert tier_quantities(API_CALLS, D("10000.5")) == (D(1000), D(9000), D("0.5"))


def test_graduated_lines_describe_each_touched_tier() -> None:
    rated = rate(API_CALLS, D(1500))
    assert [(line.quantity, line.amount_cents) for line in rated.lines] == [
        (D(1000), 100_00),
        (D(500), 40_00),
    ]
    assert rated.lines[0].description == "tier (0, 1000]"


def test_flat() -> None:
    assert rate(FlatPricing(D("0.023")), D("1500.5")).total_cents == 34_51  # 34.5115
    assert rate(FlatPricing(D("0.023")), D(0)).lines == ()


@pytest.mark.parametrize(
    ("dollars", "cents"),
    [
        ("0.005", 1),  # half-up, not banker's (which would give 0)
        ("0.015", 2),
        ("0.025", 3),  # banker's would give 2
        ("0.0049999", 0),
        ("1234567.894999", 123456789),
    ],
)
def test_rounding_rule_is_half_up(dollars: str, cents: int) -> None:
    assert to_cents(D(dollars)) == cents


def test_rounding_happens_per_line_not_per_tier_sum() -> None:
    # Two tiers each produce half a cent: each line rounds up to 1 cent -> 2 cents.
    plan = GraduatedPricing((Tier(D(1), D("0.005")), Tier(None, D("0.005"))))
    rated = rate(plan, D(2))
    assert [line.amount_cents for line in rated.lines] == [1, 1]
    assert rated.total_cents == 2


def test_minimum_commit() -> None:
    plan = PricePlan("api_calls", API_CALLS, min_commit_cents=500_00)
    under = rate_with_minimum(plan, D(1000))  # $100 of usage
    assert under.total_cents == 500_00
    assert under.lines[-1].description == "minimum commit shortfall"
    assert under.lines[-1].amount_cents == 400_00

    over = rate_with_minimum(plan, D(10000))  # $820 of usage
    assert over.total_cents == 820_00
    assert all(line.description != "minimum commit shortfall" for line in over.lines)

    assert rate_with_minimum(plan, D(0)).total_cents == 500_00
    assert minimum_commit_shortfall(500_00, 500_00) == 0


@pytest.mark.parametrize(
    "build",
    [
        lambda: GraduatedPricing(()),
        lambda: GraduatedPricing((Tier(D(10), D(1)),)),  # last tier bounded
        lambda: GraduatedPricing((Tier(D(10), D(1)), Tier(D(5), D(1)), Tier(None, D(1)))),
        lambda: GraduatedPricing((Tier(None, D(1)), Tier(None, D(1)))),
        lambda: Tier(D(0), D(1)),
        lambda: FlatPricing(D(-1)),
        lambda: FlatPricing(D("NaN")),
        lambda: FlatPricing(0.1),  # type: ignore[arg-type]  # float refused at runtime too
        lambda: PricePlan("m", FlatPricing(D(1)), min_commit_cents=-1),
    ],
)
def test_invalid_plans_are_rejected(build: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        build()  # type: ignore[operator]


@pytest.mark.parametrize("quantity", [D(-1), D("Infinity"), 5, 5.0])
def test_invalid_quantities_are_rejected(quantity: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        rate(API_CALLS, quantity)  # type: ignore[arg-type]


def test_format_cents() -> None:
    assert format_cents(123456789) == "$1,234,567.89"
    assert format_cents(5) == "$0.05"
    assert format_cents(-150) == "-$1.50"
