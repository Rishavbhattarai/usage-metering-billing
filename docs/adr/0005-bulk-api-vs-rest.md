# ADR 0005: Bulk API 2.0 for batch upserts, REST for single records

- Status: Accepted
- Date: 2026-10-07

## Context

The sync upserts hundreds to thousands of records per period. Every API call counts against
the org's daily limit.

## Decision

- Batch upserts use **Bulk API 2.0** ingest jobs through simple-salesforce: create the job,
  upload CSV, close it, poll it, then read the successful and failed record results to report
  per-record outcomes. Lookups go in as `Account__r.Billing_Customer_Id__c` columns, so
  children reference their Account by External ID without a query first.
- Single records (`upsert_account`) use one REST `PATCH /sobjects/Account/Billing_Customer_Id__c/{id}`.
- Reconciliation reads with SOQL `WHERE <external id> IN (...)`, 200 IDs per query.
- Money comes back as `Decimal` (`parse_float=Decimal`), never binary floats.

## Why not REST composite

sObject Collections upsert up to 200 records per call, which would make 1,000 records about
5 calls plus 5 for reading back. A Bulk 2.0 job costs a handful of calls whatever its size
(up to 150 MB of CSV), and its record results give per-record errors. Bulk jobs are slower to
finish (seconds of polling), which is fine for a month-end background job.

## Failure handling

- Call-level errors (network, auth, limits, 5xx) raise `SalesforceError`. The sync retries
  each call 3 times with full-jitter exponential backoff.
- If records still fail, the sync job raises. jobq retries it with its own backoff
  (`billing.sync_salesforce` gets 5 attempts), then moves it to the DLQ. Replaying from the
  DLQ re-sends only what is missing. The demo runs this round trip.

## Not verified

The client is tested against mocked HTTP (`tests/unit/test_salesforce_live_client.py`): the
JWT token request and claims, the Bulk 2.0 job sequence and result parsing, REST upsert,
SOQL, 404 handling and error mapping. It has not run against a real org yet.
