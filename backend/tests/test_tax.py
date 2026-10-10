from decimal import Decimal

import pytest


def calculate(client, auth, amount="113.00", rate="0.13", key="tax-one", **kwargs):
    return client.post(
        "/api/v1/tax-calculations",
        headers={**auth, "Idempotency-Key": key},
        json={
            "gross_amount": amount,
            "tax_rate": rate,
            "invoice_type_code": "general",
            "config_version": "tax-config-v1",
            **kwargs,
        },
    )


def test_tax_result_idempotency_and_records(client, auth):
    first = calculate(client, auth)
    second = calculate(client, auth)
    assert first.status_code == second.status_code == 201
    row = first.json()["data"]
    assert row["id"] == second.json()["data"]["id"]
    assert (row["net_amount"], row["tax_amount"]) == ("100.00", "13.00")
    assert len(client.get("/api/v1/tax-calculations", headers=auth).json()["data"]["items"]) == 1
    records = client.get("/api/v1/records", headers=auth).json()["data"]["items"]
    assert records[0]["resource_id"] == row["id"]


def test_idempotency_payload_conflict(client, auth):
    assert calculate(client, auth).status_code == 201
    result = calculate(client, auth, amount="226.00")
    assert result.status_code == 409 and result.json()["code"] == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize(
    "amount,rate",
    [("0.01", "0.13"), ("0.04", "0.13"), ("1.05", "0.06"), ("9999999999999999999999.99", "0.13")],
)
def test_decimal_rounding_and_storage(client, auth, amount, rate):
    result = calculate(client, auth, amount, rate)
    assert result.status_code == 201, result.text
    created = result.json()["data"]
    stored = client.get("/api/v1/tax-calculations/" + created["id"], headers=auth).json()["data"]
    assert stored == created
    assert Decimal(stored["net_amount"]) + Decimal(stored["tax_amount"]) == Decimal(amount)


@pytest.mark.parametrize(
    "amount,rate",
    [
        ("0", "0.13"),
        ("-1", "0.13"),
        ("1.001", "0.13"),
        ("NaN", "0.13"),
        ("1", "0.17"),
        ("1", "13"),
        ("1", "NaN"),
    ],
)
def test_reject_invalid_numbers(client, auth, amount, rate):
    assert calculate(client, auth, amount, rate).status_code == 422


def test_version_and_missing_idempotency(client, auth):
    result = calculate(client, auth, config_version="old")
    assert result.status_code == 409 and result.json()["code"] == "CONFIG_CHANGED"
    assert (
        client.post(
            "/api/v1/tax-calculations",
            headers=auth,
            json={
                "gross_amount": "1.00",
                "tax_rate": "0.13",
                "invoice_type_code": "general",
                "config_version": "tax-config-v1",
            },
        ).status_code
        == 422
    )


def test_owner_and_cursor_scope(client, auth):
    ids = [calculate(client, auth, key="tax-" + str(i)).json()["data"]["id"] for i in range(3)]
    page = client.get("/api/v1/tax-calculations?limit=2", headers=auth).json()["data"]
    assert len(page["items"]) == 2 and page["has_more"]
    next_page = client.get(
        "/api/v1/tax-calculations", headers=auth, params={"cursor": page["next_cursor"], "limit": 2}
    ).json()["data"]
    assert len(next_page["items"]) == 1
    all_ids = [x["id"] for x in page["items"] + next_page["items"]]
    assert set(all_ids) == set(ids)
    other = client.post("/api/v1/auth/dev-login", json={"account": "bob"}).json()["data"]
    other_auth = {"Authorization": "Bearer " + other["access_token"]}
    assert client.get("/api/v1/tax-calculations/" + ids[0], headers=other_auth).status_code == 404
    assert (
        client.get(
            "/api/v1/tax-calculations", headers=other_auth, params={"cursor": page["next_cursor"]}
        ).status_code
        == 400
    )
    assert (
        client.get(
            "/api/v1/tax-calculations",
            headers=auth,
            params={"cursor": page["next_cursor"], "date_from": "2026-10-01"},
        ).status_code
        == 400
    )


def test_invalid_date_and_unknown_fields(client, auth):
    assert (
        client.get(
            "/api/v1/tax-calculations?date_from=2026-10-10&date_to=2026-10-01", headers=auth
        ).status_code
        == 422
    )
    assert calculate(client, auth, user_id="another").status_code == 422
