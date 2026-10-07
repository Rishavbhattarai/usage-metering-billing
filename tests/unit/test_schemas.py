from decimal import Decimal

import pytest
from pydantic import ValidationError

from metering.schemas import UsageEventIn

VALID = {
    "event_id": "evt_1",
    "customer_id": "cust_1",
    "meter": "api_calls",
    "quantity": "1.5",
    "occurred_at": "2026-09-01T00:00:00Z",
}


def test_json_number_quantity_becomes_exact_decimal() -> None:
    event = UsageEventIn.model_validate_json(
        '{"event_id":"e","customer_id":"c","meter":"m","quantity":0.1,'
        '"occurred_at":"2026-09-01T00:00:00Z"}'
    )
    assert event.quantity == Decimal("0.1")


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        UsageEventIn.model_validate({**VALID, "surprise": 1})
