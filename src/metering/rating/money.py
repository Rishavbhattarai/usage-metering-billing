"""Money primitives. See docs/adr/0001-integer-cents-and-rounding.md.

Rules:
  * Amounts are Decimal dollars while computing and int cents once rounded. Never float.
  * Multiplication is exact (a trap fires if precision would be lost).
  * Rounding to cents happens exactly once per invoice line, ROUND_HALF_UP.
"""

from decimal import ROUND_HALF_UP, Context, Decimal, Inexact, InvalidOperation, Overflow

Cents = int

# 60 significant digits comfortably holds NUMERIC(20,6) x NUMERIC(20,6) (40 digits) exactly.
# Trapping Inexact turns any silent precision loss into an exception.
_EXACT = Context(prec=60, traps=[Inexact, InvalidOperation, Overflow])
_ROUND = Context(prec=60, rounding=ROUND_HALF_UP, traps=[InvalidOperation, Overflow])
_ONE_CENT = Decimal("0.01")


def exact_mul(a: Decimal, b: Decimal) -> Decimal:
    return _EXACT.multiply(a, b)


def exact_add(a: Decimal, b: Decimal) -> Decimal:
    return _EXACT.add(a, b)


def exact_sub(a: Decimal, b: Decimal) -> Decimal:
    return _EXACT.subtract(a, b)


def to_cents(dollars: Decimal) -> Cents:
    """Round a Decimal dollar amount to integer cents, half-up (0.005 -> 0.01)."""
    if not dollars.is_finite():
        raise ValueError(f"non-finite amount: {dollars}")
    return int(_ROUND.quantize(dollars, _ONE_CENT).scaleb(2, _ROUND))


def format_cents(cents: Cents) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) // 100:,}.{abs(cents) % 100:02d}"
