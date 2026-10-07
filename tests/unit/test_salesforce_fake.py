import pytest

from metering.salesforce import (
    Customer,
    FakeSalesforceClient,
    SalesforceClient,
    SalesforceError,
    SimpleSalesforceClient,
)


def test_fake_satisfies_protocol() -> None:
    client: SalesforceClient = FakeSalesforceClient()
    assert client.get_by_external_id("Account", "Billing_Customer_Id__c", "x") is None


def test_upsert_account_by_external_id_is_idempotent() -> None:
    sf = FakeSalesforceClient()
    first = sf.upsert_account(Customer("cust_1", "Acme"))
    second = sf.upsert_account(Customer("cust_1", "Acme Corp"))
    assert first.created and not second.created
    assert first.sf_id == second.sf_id
    assert first.sf_id is not None and len(first.sf_id) == 18
    assert sf.count("Account") == 1
    account = sf.get_by_external_id("Account", "Billing_Customer_Id__c", "cust_1")
    assert account is not None and account["Name"] == "Acme Corp"


def test_child_lookup_by_parent_external_id() -> None:
    sf = FakeSalesforceClient()
    acct = sf.upsert_account(Customer("cust_1", "Acme"))
    summary = {
        "External_Key__c": "cust_1:api_calls:2026-09",
        "Account__r": {"Billing_Customer_Id__c": "cust_1"},
        "Meter__c": "api_calls",
        "Period__c": "2026-09",
        "Quantity__c": "12345.5",
    }
    orphan = {**summary, "External_Key__c": "x", "Account__r": {"Billing_Customer_Id__c": "nope"}}
    ok, bad = sf.upsert("Usage_Summary__c", "External_Key__c", [summary, orphan])
    assert ok.success and ok.created
    assert not bad.success and bad.error is not None and "nope" in bad.error
    stored = sf.get_by_external_id("Usage_Summary__c", "External_Key__c", ok.external_id)
    assert stored is not None and stored["Account__c"] == acct.sf_id


def test_injected_failures_then_retry_succeeds() -> None:
    sf = FakeSalesforceClient()
    sf.fail_next(2)
    for _ in range(2):
        with pytest.raises(SalesforceError):
            sf.upsert_account(Customer("cust_1", "Acme"))
    assert sf.upsert_account(Customer("cust_1", "Acme")).success
    assert sf.count("Account") == 1


def test_missing_external_id_is_a_per_record_error() -> None:
    (result,) = FakeSalesforceClient().upsert("Invoice__c", "Invoice_Ext_Id__c", [{"x": 1}])
    assert not result.success


def test_real_client_is_not_implemented_yet() -> None:
    with pytest.raises(NotImplementedError):
        SimpleSalesforceClient()
