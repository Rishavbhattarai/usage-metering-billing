"""Salesforce client interface + an in-memory fake.

Everything is keyed by External IDs (see salesforce/force-app):
  Account.Billing_Customer_Id__c, Usage_Summary__c.External_Key__c,
  Invoice__c.Invoice_Ext_Id__c
Upserting by External ID is what makes the sync idempotent: sending the same record twice
updates one row instead of creating two.

Lookups are expressed by External ID too, like the real API, e.g.
  {"Account__r": {"Billing_Customer_Id__c": "cust_42"}}
"""

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

Record = Mapping[str, Any]

ACCOUNT = "Account"
ACCOUNT_EXT_ID = "Billing_Customer_Id__c"


@dataclass(frozen=True, slots=True)
class Customer:
    customer_id: str
    name: str


@dataclass(frozen=True, slots=True)
class UpsertResult:
    external_id: str
    sf_id: str | None
    created: bool
    success: bool
    error: str | None = None


class SalesforceError(Exception):
    """A call-level failure (auth, network, limits): the whole call failed, retry it."""


class SalesforceClient(Protocol):
    def upsert_account(self, customer: Customer) -> UpsertResult:
        """Upsert one Account by Billing_Customer_Id__c. Calling twice never creates two."""
        ...

    def upsert(
        self, sobject: str, external_id_field: str, records: Sequence[Record]
    ) -> list[UpsertResult]:
        """Bulk upsert by External ID. Per-record failures are reported in the results;
        call-level failures raise SalesforceError."""
        ...

    def get_by_external_id(
        self, sobject: str, external_id_field: str, value: str
    ) -> dict[str, Any] | None: ...


class FakeSalesforceClient:
    """In-memory Salesforce for tests and CI. Mirrors the behaviours the sync relies on:
    External ID upsert semantics, lookups by External ID (missing parent -> per-record error),
    and injectable call-level failures for retry tests."""

    def __init__(self) -> None:
        # sobject -> external_id_field -> external id value -> record (with "Id")
        self._data: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
        self._ids = itertools.count(1)
        self._failures: list[Exception] = []
        self.calls: list[tuple[str, str, int]] = []  # (method, sobject, n_records)

    def fail_next(self, times: int = 1, error: Exception | None = None) -> None:
        """Make the next `times` calls raise (default: SalesforceError)."""
        self._failures.extend([error or SalesforceError("injected failure")] * times)

    def _maybe_fail(self) -> None:
        if self._failures:
            raise self._failures.pop(0)

    def _new_id(self, sobject: str) -> str:
        prefix = {"Account": "001"}.get(sobject, "a00")
        return f"{prefix}{next(self._ids):015d}"  # 18 chars, like a real Salesforce Id

    def _resolve_refs(self, record: Record) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in record.items():
            if key.endswith("__r") and isinstance(value, Mapping):
                ((ext_field, ext_value),) = value.items()
                parent_obj = ACCOUNT if key == "Account__r" else key[:-3] + "__c"
                parent = self._data.get(parent_obj, {}).get(ext_field, {}).get(ext_value)
                if parent is None:
                    raise LookupError(f"{key}: no {parent_obj} with {ext_field}={ext_value}")
                out[key[:-3] + "__c"] = parent["Id"]
            else:
                out[key] = value
        return out

    def upsert(
        self, sobject: str, external_id_field: str, records: Sequence[Record]
    ) -> list[UpsertResult]:
        self.calls.append(("upsert", sobject, len(records)))
        self._maybe_fail()
        table = self._data.setdefault(sobject, {}).setdefault(external_id_field, {})
        results: list[UpsertResult] = []
        for record in records:
            ext = record.get(external_id_field)
            if not isinstance(ext, str) or not ext:
                results.append(UpsertResult("", None, False, False, "missing external id"))
                continue
            try:
                fields = self._resolve_refs(record)
            except LookupError as exc:
                results.append(UpsertResult(ext, None, False, False, str(exc)))
                continue
            existing = table.get(ext)
            if existing is None:
                table[ext] = {"Id": self._new_id(sobject), **fields}
                results.append(UpsertResult(ext, table[ext]["Id"], True, True))
            else:
                existing.update(fields)
                results.append(UpsertResult(ext, existing["Id"], False, True))
        return results

    def upsert_account(self, customer: Customer) -> UpsertResult:
        record = {ACCOUNT_EXT_ID: customer.customer_id, "Name": customer.name}
        return self.upsert(ACCOUNT, ACCOUNT_EXT_ID, [record])[0]

    def get_by_external_id(
        self, sobject: str, external_id_field: str, value: str
    ) -> dict[str, Any] | None:
        self.calls.append(("get", sobject, 1))
        self._maybe_fail()
        record = self._data.get(sobject, {}).get(external_id_field, {}).get(value)
        return dict(record) if record is not None else None

    def count(self, sobject: str) -> int:
        return sum(len(t) for t in self._data.get(sobject, {}).values())


class SimpleSalesforceClient:
    """Real client against a Salesforce org (simple-salesforce, OAuth JWT bearer flow)."""

    # YOUR TURN (see YOUR_TURN.md, push 3/3): implement the real Salesforce client.
    # - Auth: JWT bearer flow with simple-salesforce
    #   (username, consumer_key, privatekey_file, domain) read from env vars (.env.example).
    #   Never commit the private key.
    # - Implement the SalesforceClient protocol above: upsert_account (by
    #   Billing_Customer_Id__c), upsert (Bulk API 2.0 for batches), get_by_external_id.
    # - Tests stay on FakeSalesforceClient; CI never calls Salesforce.
    def __init__(self) -> None:
        raise NotImplementedError("YOUR TURN (push 3/3): real Salesforce client")

    def upsert_account(self, customer: Customer) -> UpsertResult:
        raise NotImplementedError

    def upsert(
        self, sobject: str, external_id_field: str, records: Sequence[Record]
    ) -> list[UpsertResult]:
        raise NotImplementedError

    def get_by_external_id(
        self, sobject: str, external_id_field: str, value: str
    ) -> dict[str, Any] | None:
        raise NotImplementedError
