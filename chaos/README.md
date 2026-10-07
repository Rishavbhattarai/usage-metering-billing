# Chaos demos (placeholder)

Planned for Week 4–5. Each one will be a script plus a short recording:

1. **Kill Postgres during ingestion.** `docker compose kill postgres` while the load
   generator runs. Batches fail atomically, the producer retries, and the final row count
   equals the number of unique events (no loss, no double count).
2. **Salesforce sync failure.** Inject failures (`FakeSalesforceClient.fail_next`, or revoke
   the Connected App in the dev org) during a sync. The job retries with backoff, goes to
   Project 1's DLQ, is replayed, and reconciliation reports zero drift.
3. **Kill a worker mid-invoice.** Closing a month twice still gives one invoice
   (`UNIQUE(customer_id, period)`).
