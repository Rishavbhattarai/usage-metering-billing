"""Billing periods are calendar months in UTC, written 'YYYY-MM'."""

import re
from datetime import UTC, date, datetime

_PERIOD = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")


def parse_period(period: str) -> tuple[int, int]:
    m = _PERIOD.match(period)
    if not m:
        raise ValueError(f"period must look like YYYY-MM, got {period!r}")
    return int(m.group(1)), int(m.group(2))


def period_start(period: str) -> datetime:
    year, month = parse_period(period)
    return datetime(year, month, 1, tzinfo=UTC)


def next_period(period: str) -> str:
    year, month = parse_period(period)
    return f"{year + month // 12}-{month % 12 + 1:02d}"


def prev_period(period: str) -> str:
    year, month = parse_period(period)
    return f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"


def period_end(period: str) -> datetime:
    """Exclusive end: the first instant of the next period."""
    return period_start(next_period(period))


def period_of(ts: datetime | date) -> str:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        ts = ts.astimezone(UTC)
    return f"{ts.year}-{ts.month:02d}"
