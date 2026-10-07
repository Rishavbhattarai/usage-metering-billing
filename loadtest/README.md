# Load test

`generate_events.py` produces a deterministic event stream (per `--seed`) with:
- **duplicates**: `--dup-rate` of the events are re-sends of earlier ones (same `event_id`)
- **late events**: `--late-rate` of the new events have `occurred_at` in the previous month

```bash
docker compose up -d --build
python loadtest/generate_events.py --count 100000 --url http://localhost:8000
# {"received": 100000, "accepted": ..., "duplicates": ..., "seconds": ..., "events_per_sec": ...}

# Re-run with the same seed: everything is a duplicate, and nothing new is stored.
python loadtest/generate_events.py --count 100000 --url http://localhost:8000

# Or write JSON Lines without a server:
python loadtest/generate_events.py --count 10000 --out events.jsonl
```

The script uses one client sending sequential batches, so it measures latency-bound
throughput. For published numbers (Week 5), add concurrency (or use k6/Locust), record p50/p99,
and state the hardware.

## Results

Not measured yet. Fill this in at Week 5 with the hardware, Docker CPU/RAM, events/sec and p50/p99.
