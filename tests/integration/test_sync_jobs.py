"""Salesforce sync + reconciliation (against the fake) and the job chain via the API."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import httpx
import pytest

from metering.ingestion import ingest_events
from metering.invoicing import close_period
from metering.runtime import Runtime
from metering.salesforce.client import FakeSalesforceClient
from metering.salesforce.sync import SyncFailedError, reconcile_period, sync_period
from metering.schemas import UsageEventIn

pytestmark = pytest.mark.integration


async def billed_august(rt: Runtime) -> None:
    events = [
        UsageEventIn(
            event_id=f"e{i}",
            customer_id=f"cust_{i % 3}",
            meter="api_calls" if i % 2 else "gb_stored",
            quantity=D(100 + i),
            occurred_at=datetime(2026, 8, 1 + i, tzinfo=UTC),
        )
        for i in range(12)
    ]
    async with rt.sessions() as session, session.begin():
        await ingest_events(session, events)
    async with rt.sessions() as session, session.begin():
        await close_period(session, "2026-08", cutoff_lag=timedelta(0))


async def test_zero_drift_after_forced_failure_and_retry(
    rt: Runtime, fake_sf: FakeSalesforceClient
) -> None:
    await billed_august(rt)
    # Accounts sync, then every try at Usage_Summary__c fails (3 attempts with backoff).
    fake_sf.fail_next(3, after=1)
    with pytest.raises(SyncFailedError):
        await sync_period(rt.sessions, fake_sf, "2026-08", attempts=3, backoff_base=0.001)

    drift = await reconcile_period(rt.sessions, fake_sf, "2026-08")
    assert drift.drift > 0
    assert {m["sobject"] for m in drift.missing} == {"Usage_Summary__c", "Invoice__c"}

    retry = await sync_period(rt.sessions, fake_sf, "2026-08", backoff_base=0.001)
    assert retry.skipped_unchanged["Account"] == 3  # only what was missing is re-sent
    clean = await reconcile_period(rt.sessions, fake_sf, "2026-08")
    assert clean.drift == 0
    assert clean.ledger_total_cents == clean.salesforce_total_cents > 0
    assert fake_sf.count("Account") == 3


async def test_late_adjustments_keep_both_periods_in_sync(
    rt: Runtime, fake_sf: FakeSalesforceClient
) -> None:
    """Re-syncing or reconciling an old period after a later period booked late usage for
    it must not roll the old usage summary back."""
    await billed_august(rt)
    await sync_period(rt.sessions, fake_sf, "2026-08")
    late = UsageEventIn(
        event_id="late-1",
        customer_id="cust_1",
        meter="api_calls",
        quantity=D(50),
        occurred_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    async with rt.sessions() as session, session.begin():
        await ingest_events(session, [late])
    async with rt.sessions() as session, session.begin():
        await close_period(session, "2026-09", cutoff_lag=timedelta(0))
    await sync_period(rt.sessions, fake_sf, "2026-09")

    summary = fake_sf.get_by_external_id(
        "Usage_Summary__c", "External_Key__c", "cust_1:api_calls:2026-08"
    )
    assert (
        summary is not None and D(summary["Quantity__c"]) == D(101 + 107) + 50
    )  # events 1 and 7, plus the late 50
    for period in ("2026-08", "2026-09"):
        assert (await reconcile_period(rt.sessions, fake_sf, period)).drift == 0
    resync = await sync_period(rt.sessions, fake_sf, "2026-08")
    assert sum(resync.sent.values()) == 0  # nothing to roll back


async def test_transient_failures_are_absorbed_by_backoff(
    rt: Runtime, fake_sf: FakeSalesforceClient
) -> None:
    await billed_august(rt)
    fake_sf.fail_next(2)
    result = await sync_period(rt.sessions, fake_sf, "2026-08", attempts=3, backoff_base=0.001)
    assert not any(result.failed.values())
    assert (await reconcile_period(rt.sessions, fake_sf, "2026-08")).drift == 0


async def test_manual_edit_in_salesforce_is_detected_and_repaired(
    rt: Runtime, fake_sf: FakeSalesforceClient
) -> None:
    await billed_august(rt)
    await sync_period(rt.sessions, fake_sf, "2026-08")
    fake_sf.tamper("Invoice__c", "Invoice_Ext_Id__c", "inv_2026-08_cust_0", Total__c="1.00")

    report = await reconcile_period(rt.sessions, fake_sf, "2026-08", repair=True)
    assert [m["external_id"] for m in report.mismatched] == ["inv_2026-08_cust_0"]
    resync = await sync_period(rt.sessions, fake_sf, "2026-08")
    assert resync.sent["Invoice__c"] == 1
    assert (await reconcile_period(rt.sessions, fake_sf, "2026-08")).drift == 0


async def test_close_via_api_runs_close_sync_reconcile_chain(
    rt: Runtime, client: httpx.AsyncClient, fake_sf: FakeSalesforceClient
) -> None:
    events = [
        {
            "event_id": f"api-{i}",
            "customer_id": "acme",
            "meter": "api_calls",
            "quantity": "600",
            "occurred_at": f"2026-08-0{i + 1}T00:00:00Z",
        }
        for i in range(2)
    ]
    assert (await client.post("/v1/events/batch", json={"events": events})).status_code == 200

    resp = await client.post("/v1/periods/2026-08/close")
    assert resp.status_code == 202
    job = (await client.get(f"/v1/jobs/{resp.json()['job_id']}")).json()
    assert job["status"] == "succeeded", job
    assert job["result"]["total_cents"] == 100_00 + 200 * 8  # 1,200 calls

    # Same idempotency key: closing twice returns the same job.
    again = await client.post("/v1/periods/2026-08/close")
    assert again.json()["job_id"] == resp.json()["job_id"]

    recon = (await client.get("/v1/periods/2026-08/reconciliation")).json()
    assert recon["drift"] == 0 and recon["salesforce_total_cents"] == 116_00
    assert fake_sf.count("Invoice__c") == 1

    invoice = (await client.get("/v1/invoices/inv_2026-08_acme")).json()
    assert invoice["total_cents"] == 116_00 and len(invoice["content_hash"]) == 64
    assert (await client.get("/v1/invoices/inv_2026-08_acme/rerate")).json()["identical"]
    assert (await client.get("/v1/periods/2026-08/rerate")).json()["identical"] == 1


async def test_failed_sync_job_is_reported_failed(
    rt: Runtime, client: httpx.AsyncClient, fake_sf: FakeSalesforceClient
) -> None:
    await billed_august(rt)
    fake_sf.fail_next(100)
    job_id = (await client.post("/v1/periods/2026-08/sync")).json()["job_id"]
    job = (await client.get(f"/v1/jobs/{job_id}")).json()
    assert job["status"] == "failed" and "SyncFailedError" in job["error"]


async def test_dashboard_and_usage_endpoints(rt: Runtime, client: httpx.AsyncClient) -> None:
    await billed_august(rt)
    assert (await client.post("/v1/aggregate")).json()["dirty_cells"] > 0
    usage = (await client.get("/v1/customers/cust_1/usage?period=2026-08")).json()
    assert usage["meters"] and usage["estimate_cents"] > 0
    hourly = (
        await client.get("/v1/customers/cust_1/usage?period=2026-08&granularity=hourly")
    ).json()
    assert sum(D(r["quantity"]) for r in hourly["rows"]) == sum(
        D(m["quantity"]) for m in usage["meters"]
    )

    page = await client.get("/dashboard")
    assert page.status_code == 200 and "cust_1" in page.text and "2026-08" in page.text
    page = await client.get("/dashboard/customers/cust_1?period=2026-08")
    assert page.status_code == 200 and "inv_2026-08_cust_1" in page.text and "hx-get" in page.text
    assert (await client.get("/dashboard/customers/cust_1/usage?period=2026-08")).status_code == 200
    assert (await client.get("/dashboard/invoices/inv_2026-08_cust_1")).status_code == 200
    assert (await client.get("/static/htmx-2.0.4.min.js")).status_code == 200
