# Load tests and demo

All three scripts talk to the running compose stack over HTTP. Results from the last run are
in `results/`, and the hardware is listed in the main README.

| Script | What it measures |
|---|---|
| `bench.py` | Ingestion throughput and batch latency with N requests in flight, plus event-to-aggregate lag from probe events. |
| `demo.py` | The full month-end flow with 1M events: ingest, aggregate, close via jobq, sync with a forced failure, late events, reconcile, DLQ round trip, re-rate, replay. |
| `generate_events.py` | Deterministic event stream with duplicates and late events (JSON Lines or POST). |

```bash
# Short close cutoff so the demo doesn't wait a minute before each close.
CLOSE_CUTOFF_LAG_SECONDS=3 docker compose up -d --build --wait
python loadtest/bench.py --count 1000000 --concurrency 8 --out loadtest/results/ingest-1m.json
python loadtest/bench.py --count 0 --probe-seconds 30 --out loadtest/results/lag-idle.json
python loadtest/demo.py --events 1000000 --fail-sync --dlq --replay --out loadtest/results/demo-1m.json
docker compose down -v
```

`demo.py` uses `docker compose exec` to inject Salesforce failures and to run the replay
check, so run it from the repo root.
