"""Invoice generation (Week 3).

TODO(week 3): month-end invoice job on the JobQueue (metering.jobs.queue):
  - one invoice per (customer_id, period), enforced by UNIQUE(customer_id, period)
  - rate usage with metering.rating.rate_with_minimum; store BIGINT cents per line
  - late events after period close -> adjustment lines on the next invoice
  - re-rating: recompute a period from raw events and diff against the stored invoice
"""
