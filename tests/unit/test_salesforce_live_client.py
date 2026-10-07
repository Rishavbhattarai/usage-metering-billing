"""The real Salesforce client, exercised against mocked HTTP (no org needed).

These pin the wire protocol simple-salesforce speaks for us: the JWT bearer token request,
a Bulk API 2.0 ingest job (create, upload CSV, close, poll, read results), a REST upsert and
a SOQL query. They don't prove a live org accepts it; see docs/salesforce-setup.md.
"""

import json
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs

import jwt
import pytest
import responses
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from metering.salesforce.client import Customer, SalesforceError, SimpleSalesforceClient

TOKEN_URL = "https://login.salesforce.com/services/oauth2/token"
INSTANCE = "https://acme-dev-ed.develop.my.salesforce.com"
API = f"{INSTANCE}/services/data/v62.0"
JOB = f"{API}/jobs/ingest/750XX0000000001"


@pytest.fixture
def key_pair(tmp_path: Path) -> tuple[Path, rsa.RSAPublicKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "server.key"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path, key.public_key()


@pytest.fixture(autouse=True)
def no_bulk_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("simple_salesforce.bulk2.sleep", lambda _s: None)


def login(rsps: responses.RequestsMock, key_path: Path) -> SimpleSalesforceClient:
    rsps.post(TOKEN_URL, json={"access_token": "00Dtoken", "instance_url": INSTANCE})
    return SimpleSalesforceClient.from_jwt("bot@acme.dev", "CONSUMER_KEY", str(key_path))


@responses.activate
def test_jwt_bearer_login_signs_the_expected_claims(
    key_pair: tuple[Path, rsa.RSAPublicKey],
) -> None:
    key_path, public_key = key_pair
    login(responses.mock, key_path)
    body = parse_qs(str(responses.calls[0].request.body))
    assert body["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    claims = jwt.decode(
        body["assertion"][0],
        public_key,
        algorithms=["RS256"],
        audience="https://login.salesforce.com",
    )
    assert (claims["iss"], claims["sub"]) == ("CONSUMER_KEY", "bot@acme.dev")


@pytest.mark.filterwarnings("ignore::UserWarning:simple_salesforce.login")
@responses.activate
def test_login_failure_is_a_salesforce_error(key_pair: tuple[Path, rsa.RSAPublicKey]) -> None:
    responses.post(
        TOKEN_URL,
        status=400,
        json={"error": "invalid_grant", "error_description": "user hasn't approved this consumer"},
    )
    with pytest.raises(SalesforceError, match="JWT login failed"):
        SimpleSalesforceClient.from_jwt("bot@acme.dev", "CONSUMER_KEY", str(key_pair[0]))


@responses.activate
def test_bulk2_upsert_reports_per_record_outcomes(key_pair: tuple[Path, rsa.RSAPublicKey]) -> None:
    client = login(responses.mock, key_pair[0])
    responses.post(f"{API}/jobs/ingest", json={"id": "750XX0000000001", "state": "Open"})
    responses.put(f"{JOB}/batches", status=201)
    responses.patch(JOB, json={"id": "750XX0000000001", "state": "UploadComplete"})
    responses.get(
        JOB,
        json={"state": "JobComplete", "numberRecordsFailed": 1, "numberRecordsProcessed": 2},
    )
    responses.get(
        f"{JOB}/successfulResults",
        body='"sf__Id","sf__Created",Invoice_Ext_Id__c,Account__r.Billing_Customer_Id__c,Total__c\n'
        '"a01XX0000000001","true",inv_1,c1,12.34\n',
    )
    responses.get(
        f"{JOB}/failedResults",
        body='"sf__Id","sf__Error",Invoice_Ext_Id__c,Account__r.Billing_Customer_Id__c,Total__c\n'
        '"","INVALID_FIELD:Foreign key external ID: c9 not found",inv_2,c9,1.00\n',
    )

    results = client.upsert(
        "Invoice__c",
        "Invoice_Ext_Id__c",
        [
            {
                "Invoice_Ext_Id__c": "inv_1",
                "Account__r": {"Billing_Customer_Id__c": "c1"},
                "Total__c": "12.34",
            },
            {
                "Invoice_Ext_Id__c": "inv_2",
                "Account__r": {"Billing_Customer_Id__c": "c9"},
                "Total__c": "1.00",
            },
        ],
    )
    assert [(r.external_id, r.success, r.created, r.sf_id) for r in results] == [
        ("inv_1", True, True, "a01XX0000000001"),
        ("inv_2", False, False, None),
    ]
    assert results[1].error is not None and "c9 not found" in results[1].error

    create = json.loads(responses.calls[1].request.body)  # type: ignore[arg-type]
    assert create["operation"] == "upsert"
    assert create["object"] == "Invoice__c"
    assert create["externalIdFieldName"] == "Invoice_Ext_Id__c"
    uploaded = responses.calls[2].request.body
    assert isinstance(uploaded, bytes)
    header = set(uploaded.splitlines()[0].decode().split(","))
    assert header == {"Invoice_Ext_Id__c", "Account__r.Billing_Customer_Id__c", "Total__c"}


@responses.activate
def test_call_level_failure_raises_for_retry(key_pair: tuple[Path, rsa.RSAPublicKey]) -> None:
    client = login(responses.mock, key_pair[0])
    responses.post(f"{API}/jobs/ingest", status=503, json=[{"message": "unavailable"}])
    with pytest.raises(SalesforceError, match="bulk upsert Account"):
        client.upsert("Account", "Billing_Customer_Id__c", [{"Billing_Customer_Id__c": "c1"}])


@responses.activate
def test_upsert_account_uses_rest_patch_by_external_id(
    key_pair: tuple[Path, rsa.RSAPublicKey],
) -> None:
    client = login(responses.mock, key_pair[0])
    url = f"{API}/sobjects/Account/Billing_Customer_Id__c/cust_1"
    responses.patch(url, status=201, json={"id": "001XX0000000001", "success": True})
    created = client.upsert_account(Customer("cust_1", "Acme"))
    responses.patch(url, status=204)
    updated = client.upsert_account(Customer("cust_1", "Acme Corp"))
    assert (created.created, created.sf_id) == (True, "001XX0000000001")
    assert (updated.created, updated.success) == (False, True)
    assert json.loads(responses.calls[-1].request.body) == {"Name": "Acme Corp"}  # type: ignore[arg-type]


@responses.activate
def test_fetch_queries_by_external_ids_with_exact_decimals(
    key_pair: tuple[Path, rsa.RSAPublicKey],
) -> None:
    client = login(responses.mock, key_pair[0])
    responses.get(
        f"{API}/query/",
        body=json.dumps(
            {
                "totalSize": 1,
                "done": True,
                "records": [
                    {
                        "attributes": {"type": "Invoice__c"},
                        "Invoice_Ext_Id__c": "inv_1",
                        "Total__c": 0.30,
                        "Account__r": {"Billing_Customer_Id__c": "c1"},
                    }
                ],
            }
        ).replace("0.3", "0.30"),
    )
    got = client.fetch(
        "Invoice__c",
        "Invoice_Ext_Id__c",
        ["inv_1", "inv_x"],
        ["Total__c", "Account__r.Billing_Customer_Id__c"],
    )
    assert got == {
        "inv_1": {"Total__c": Decimal("0.30"), "Account__r.Billing_Customer_Id__c": "c1"}
    }
    soql = parse_qs(responses.calls[-1].request.url.split("?", 1)[1])["q"][0]  # type: ignore[union-attr]
    assert soql == (
        "SELECT Invoice_Ext_Id__c, Total__c, Account__r.Billing_Customer_Id__c FROM Invoice__c "
        "WHERE Invoice_Ext_Id__c IN ('inv_1', 'inv_x')"
    )


@responses.activate
def test_get_by_external_id_returns_none_on_404(key_pair: tuple[Path, rsa.RSAPublicKey]) -> None:
    client = login(responses.mock, key_pair[0])
    responses.get(
        f"{API}/sobjects/Invoice__c/Invoice_Ext_Id__c/nope",
        status=404,
        json=[{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}],
    )
    assert client.get_by_external_id("Invoice__c", "Invoice_Ext_Id__c", "nope") is None


def test_from_env_names_missing_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("SF_USERNAME", "SF_CONSUMER_KEY", "SF_PRIVATE_KEY_PATH"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(SalesforceError, match="SF_USERNAME"):
        SimpleSalesforceClient.from_env()
