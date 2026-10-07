# ADR 0004: Sync aggregates to Salesforce, never raw events

- Status: Accepted
- Date: 2026-10-07

## Context

A Developer Edition org has a daily API request limit (about 15k per 24 h) and about 5 MB of
data storage. The 1M-event demo alone would exceed both many times over if events were synced.

## Decision

Sync three record types, each upserted by an External ID:

| Object | External ID | One record per |
|--------|-------------|----------------|
| `Account` | `Billing_Customer_Id__c` | customer |
| `Usage_Summary__c` | `External_Key__c` = `customer:meter:YYYY-MM` | customer, meter, usage period |
| `Invoice__c` | `Invoice_Ext_Id__c` = `inv_YYYY-MM_customer` | invoice |

- The ledger is the source of truth. Salesforce is a downstream copy that reconciliation checks.
- A usage summary's quantity is everything billed for that usage period on any invoice,
  including later late-event adjustments. Re-syncing an old period never rolls back a later
  adjustment. A test covers this, after the demo caught the bug.
- `sf_sync_log` stores the hash of the last payload that synced per record, and unchanged
  records are skipped. Re-running a sync after a partial failure only re-sends what is missing.
- Reconciliation fetches every expected record by External ID, compares the fields, and
  stores a report (missing, mismatched, ledger total and Salesforce total). `repair` clears
  the sync log for drifted records so the next sync re-sends them.

## Consequences

- In the 1M-event demo, two months of usage for 100 customers became 700 Salesforce records
  (100 Accounts, 400 usage summaries, 200 invoices). The two syncs sent 900 upserts, because
  September's late-event adjustments updated the 200 August summaries.
- Line items stay in the ledger. Salesforce users see totals and per-meter quantities, not
  invoice lines.
