"""Request/response models for the ingestion API."""

from decimal import Decimal
from typing import Annotated

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

Identifier = Annotated[
    str, StringConstraints(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:\-]+$")
]


class UsageEventIn(BaseModel):
    """One usage event. `event_id` is chosen by the producer and is the idempotency key:
    re-sending the same event_id is always safe and never double-counts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: Identifier
    customer_id: Identifier
    meter: Identifier
    # Matches NUMERIC(20, 6). JSON numbers and numeric strings are parsed straight into
    # Decimal; send strings ("12.5") if your producer's JSON encoder uses binary floats.
    quantity: Annotated[Decimal, Field(ge=0, max_digits=20, decimal_places=6)]
    occurred_at: AwareDatetime


class BatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[UsageEventIn] = Field(min_length=1)


class IngestResult(BaseModel):
    """`accepted` = newly stored. `duplicates` = event_ids already stored (or repeated inside
    this request). received == accepted + duplicates always holds."""

    received: int
    accepted: int
    duplicates: int


class UsageEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event_id: str
    customer_id: str
    meter: str
    quantity: Decimal
    occurred_at: AwareDatetime
    received_at: AwareDatetime
