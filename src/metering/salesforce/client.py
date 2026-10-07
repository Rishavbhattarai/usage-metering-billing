"""Salesforce client interface, an in-memory fake and the real client.

Everything is keyed by External IDs (see salesforce/force-app):
  Account.Billing_Customer_Id__c, Usage_Summary__c.External_Key__c,
  Invoice__c.Invoice_Ext_Id__c
Upserting by External ID is what makes the sync idempotent: sending the same record twice
updates one row instead of creating two.

Lookups are expressed by External ID too, like the real API, e.g.
  {"Account__r": {"Billing_Customer_Id__c": "cust_42"}}
"""

import csv
import fcntl
import io
import itertools
import json
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
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

    def fetch(
        self,
        sobject: str,
        external_id_field: str,
        values: Sequence[str],
        fields: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        """Records whose External ID is in `values`, keyed by External ID. `fields` may use
        relationship paths like 'Account__r.Billing_Customer_Id__c' (returned flattened under
        that dotted key)."""
        ...


class FakeSalesforceClient:
    """In-memory Salesforce for tests, CI and the local demo. It mirrors what the sync relies
    on: External ID upsert semantics, parent lookups by External ID (missing parent ->
    per-record error) and injectable call-level failures.

    With `state_path`, the org is persisted to a JSON file (under an exclusive file lock)
    so several processes, e.g. a jobq worker and the CLI, share one fake org.
    """

    def __init__(self, state_path: str | Path | None = None) -> None:
        self._path = Path(state_path) if state_path else None
        # sobject -> external_id_field -> external id value -> record (with "Id")
        self._data: dict[str, dict[str, dict[str, dict[str, Any]]]] = {}
        self._next_id = 1
        self._schedule: list[bool] = []  # per upcoming call: True = fail
        self.calls: list[tuple[str, str, int]] = []  # (method, sobject, n_records)

    # -- persistence ---------------------------------------------------------------------
    @contextmanager
    def _state(self) -> Iterator[None]:
        if self._path is None:
            yield
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self._path.exists():
                state = json.loads(self._path.read_text())
                self._data = state["data"]
                self._next_id = state["next_id"]
                self._schedule = state["schedule"]
            try:
                yield
            finally:  # also persist when an injected failure was consumed
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(
                        {"data": self._data, "next_id": self._next_id, "schedule": self._schedule},
                        sort_keys=True,
                        default=str,
                    )
                )
                os.replace(tmp, self._path)

    # -- failure injection -----------------------------------------------------------------
    def fail_next(self, times: int = 1, after: int = 0) -> None:
        """Let `after` calls succeed, then make the next `times` calls raise SalesforceError."""
        with self._state():
            self._schedule.extend([False] * after + [True] * times)

    def clear_failures(self) -> None:
        with self._state():
            self._schedule = []

    def _maybe_fail(self) -> None:
        if self._schedule and self._schedule.pop(0):
            raise SalesforceError("injected failure")

    # -- org operations ----------------------------------------------------------------------
    def _new_id(self, sobject: str) -> str:
        prefix = {"Account": "001"}.get(sobject, "a00")
        n, self._next_id = self._next_id, self._next_id + 1
        return f"{prefix}{n:015d}"  # 18 chars, like a real Salesforce Id

    def _parent_object(self, rel: str) -> str:
        return ACCOUNT if rel == "Account__r" else rel[:-3] + "__c"

    def _find(self, sobject: str, ext_field: str, ext_value: Any) -> dict[str, Any] | None:
        return self._data.get(sobject, {}).get(ext_field, {}).get(ext_value)

    def _resolve_refs(self, record: Record) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in record.items():
            if key.endswith("__r") and isinstance(value, Mapping):
                ((ext_field, ext_value),) = value.items()
                parent = self._find(self._parent_object(key), ext_field, ext_value)
                if parent is None:
                    raise LookupError(
                        f"{key}: no {self._parent_object(key)} with {ext_field}={ext_value}"
                    )
                out[key[:-3] + "__c"] = parent["Id"]
            else:
                out[key] = str(value) if isinstance(value, Decimal) else value
        return out

    def upsert(
        self, sobject: str, external_id_field: str, records: Sequence[Record]
    ) -> list[UpsertResult]:
        with self._state():
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
        with self._state():
            self.calls.append(("get", sobject, 1))
            self._maybe_fail()
            record = self._find(sobject, external_id_field, value)
            return dict(record) if record is not None else None

    def fetch(
        self,
        sobject: str,
        external_id_field: str,
        values: Sequence[str],
        fields: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        with self._state():
            self.calls.append(("fetch", sobject, len(values)))
            self._maybe_fail()
            out: dict[str, dict[str, Any]] = {}
            for value in values:
                record = self._find(sobject, external_id_field, value)
                if record is None:
                    continue
                row: dict[str, Any] = {}
                for f in fields:
                    if "." in f:
                        rel, parent_field = f.split(".", 1)
                        parent_id = record.get(rel[:-3] + "__c")
                        parent = self._by_id(self._parent_object(rel), parent_id)
                        row[f] = parent.get(parent_field) if parent else None
                    else:
                        row[f] = record.get(f)
                out[value] = row
            return out

    def _by_id(self, sobject: str, sf_id: Any) -> dict[str, Any] | None:
        for table in self._data.get(sobject, {}).values():
            for rec in table.values():
                if rec["Id"] == sf_id:
                    return rec
        return None

    def tamper(self, sobject: str, external_id_field: str, value: str, **fields: Any) -> None:
        """Edit a record behind the sync's back (simulates a manual edit in Salesforce)."""
        with self._state():
            record = self._find(sobject, external_id_field, value)
            if record is None:
                raise KeyError(value)
            record.update(fields)

    def count(self, sobject: str) -> int:
        with self._state():
            return sum(len(t) for t in self._data.get(sobject, {}).values())

    def records(self, sobject: str) -> list[dict[str, Any]]:
        with self._state():
            return [dict(r) for t in self._data.get(sobject, {}).values() for r in t.values()]


# --------------------------------------------------------------------------------------------
# Real client
# --------------------------------------------------------------------------------------------

SOQL_CHUNK = 200  # External IDs per SOQL IN (...) clause; keeps queries well under limits.


def _soql_quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _flatten(record: Record) -> dict[str, str]:
    """Bulk API 2.0 takes CSV: relationship lookups become 'Account__r.Field' columns."""
    flat: dict[str, str] = {}
    for key, value in record.items():
        if isinstance(value, Mapping):
            for sub, v in value.items():
                flat[f"{key}.{sub}"] = "" if v is None else str(v)
        else:
            flat[key] = "" if value is None else str(value)
    return flat


def _flatten_result(record: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in fields:
        node: Any = record
        for part in f.split("."):
            node = node.get(part) if isinstance(node, Mapping) else None
        out[f] = node
    return out


class SimpleSalesforceClient:
    """Salesforce over REST + Bulk API 2.0, via simple-salesforce, authenticated with the
    OAuth 2.0 JWT bearer flow (no password, no browser; see docs/salesforce-setup.md).

    - upsert(): Bulk API 2.0 ingest job per call (create job, upload CSV, close, poll),
      then reads the job's successful/failed record results to report per-record outcomes.
    - upsert_account(): one REST PATCH on /sobjects/Account/Billing_Customer_Id__c/{id}.
    - fetch(): SOQL `WHERE ext IN (...)` in chunks (reconciliation).
    Call-level failures are raised as SalesforceError so callers can retry them.
    """

    def __init__(self, sf: Any, bulk_poll_seconds: int = 2) -> None:
        self._sf = sf
        self._poll = bulk_poll_seconds

    @classmethod
    def from_jwt(
        cls,
        username: str,
        consumer_key: str,
        private_key_path: str,
        domain: str = "login",
        api_version: str = "62.0",
    ) -> "SimpleSalesforceClient":
        from simple_salesforce.api import Salesforce
        from simple_salesforce.exceptions import SalesforceError as SdkError

        try:
            sf = Salesforce(
                username=username,
                consumer_key=consumer_key,
                privatekey_file=os.path.expanduser(private_key_path),
                domain=domain,
                version=api_version,
                parse_float=Decimal,  # money comes back exact, not as binary floats
            )
        except SdkError as exc:
            raise SalesforceError(f"JWT login failed: {exc}") from exc
        return cls(sf)

    @classmethod
    def from_env(cls) -> "SimpleSalesforceClient":
        missing = [
            k
            for k in ("SF_USERNAME", "SF_CONSUMER_KEY", "SF_PRIVATE_KEY_PATH")
            if not os.environ.get(k)
        ]
        if missing:
            raise SalesforceError(f"missing settings: {', '.join(missing)} (see .env.example)")
        return cls.from_jwt(
            os.environ["SF_USERNAME"],
            os.environ["SF_CONSUMER_KEY"],
            os.environ["SF_PRIVATE_KEY_PATH"],
            os.environ.get("SF_DOMAIN", "login"),
        )

    @contextmanager
    def _wrap(self, what: str) -> Iterator[None]:
        import requests
        from simple_salesforce.exceptions import SalesforceError as SdkError
        from simple_salesforce.exceptions import SalesforceOperationError

        try:
            yield
        except (SdkError, SalesforceOperationError, requests.RequestException) as exc:
            raise SalesforceError(f"{what}: {exc}") from exc

    def upsert(
        self, sobject: str, external_id_field: str, records: Sequence[Record]
    ) -> list[UpsertResult]:
        if not records:
            return []
        flat = [_flatten(r) for r in records]
        with self._wrap(f"bulk upsert {sobject}"):
            handler = getattr(self._sf.bulk2, sobject)
            jobs = handler.upsert(
                records=flat, external_id_field=external_id_field, wait=self._poll
            )
            outcome: dict[str, UpsertResult] = {}
            for job in jobs:
                for row in csv.DictReader(
                    io.StringIO(handler.get_successful_records(job["job_id"]))
                ):
                    ext = row[external_id_field]
                    outcome[ext] = UpsertResult(
                        ext, row["sf__Id"], row["sf__Created"].lower() == "true", True
                    )
                failed_csv = handler.get_failed_records(job["job_id"])
                for row in csv.DictReader(io.StringIO(failed_csv)):
                    ext = row[external_id_field]
                    outcome[ext] = UpsertResult(
                        ext, row.get("sf__Id") or None, False, False, row["sf__Error"]
                    )
        return [
            outcome.get(
                r[external_id_field],
                UpsertResult(r[external_id_field], None, False, False, "not processed"),
            )
            for r in flat
        ]

    def upsert_account(self, customer: Customer) -> UpsertResult:
        with self._wrap("upsert Account"):
            resp = self._sf.Account.upsert(
                f"{ACCOUNT_EXT_ID}/{customer.customer_id}",
                {"Name": customer.name},
                raw_response=True,
            )
        created = resp.status_code == 201
        sf_id = resp.json().get("id") if created and resp.content else None
        return UpsertResult(customer.customer_id, sf_id, created, True)

    def get_by_external_id(
        self, sobject: str, external_id_field: str, value: str
    ) -> dict[str, Any] | None:
        from simple_salesforce.exceptions import SalesforceResourceNotFound

        with self._wrap(f"get {sobject}"):
            try:
                record: dict[str, Any] = getattr(self._sf, sobject).get_by_custom_id(
                    external_id_field, value
                )
            except SalesforceResourceNotFound:
                return None
        return record

    def fetch(
        self,
        sobject: str,
        external_id_field: str,
        values: Sequence[str],
        fields: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        select_fields = list(dict.fromkeys([external_id_field, *fields]))
        for chunk in itertools.batched(values, SOQL_CHUNK):
            soql = (
                f"SELECT {', '.join(select_fields)} FROM {sobject} "
                f"WHERE {external_id_field} IN ({', '.join(map(_soql_quote, chunk))})"
            )
            with self._wrap(f"query {sobject}"):
                result = self._sf.query_all(soql)
            for rec in result["records"]:
                out[rec[external_id_field]] = _flatten_result(rec, fields)
        return out
