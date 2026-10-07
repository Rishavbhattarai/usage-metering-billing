# Failure demos

`loadtest/demo.py --fail-sync --dlq` runs these against the compose stack. Results are in
`loadtest/results/demo-1m.json`.

| Failure | What happens | Evidence |
|---|---|---|
| Salesforce fails the first 3 API calls of a sync | The sync's own retries run out, the job attempt fails, and jobq retries it with backoff. The second attempt succeeds and reconciliation shows zero drift. | `close_1.sync_attempts = 2`, drift 0 |
| Someone edits an invoice total in Salesforce | Reconciliation reports 1 mismatched record, and `repair` marks it for re-sync. | `dlq.drift_after_tamper = 1` |
| Salesforce is down for longer than the retry budget | The sync job fails 5 times and jobq moves it to the DLQ. After the outage, `POST /dlq/{id}/replay` re-runs it, and the chained reconcile reports zero drift. | `dlq.in_dlq = true`, `drift_after_replay = 0` |
| A job is delivered twice | Close, sync and reconcile are idempotent, and chained jobs use idempotency keys. | integration tests |
| Ingest batch retried after a timeout | Rows already stored count as duplicates. | 10k / 10% duplicates test, replay of the whole stream stores 0 |

Not scripted yet: killing a worker mid-close. The close is a single transaction, so a killed
worker leaves nothing behind and jobq's lease expiry re-delivers the job. This hasn't been
recorded as a demo.
