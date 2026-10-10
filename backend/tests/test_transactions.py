from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from app.core.errors import BusinessError
from app.db.models import IdempotencyRecord, TaxCalculation
from app.schemas.tax import TaxInput
from app.services import idempotency, tax


def test_resource_and_idempotency_roll_back_together(app, client, auth):
    user_id = client.get("/api/v1/users/me", headers=auth).json()["data"]["id"]
    body = TaxInput(
        gross_amount="113.00",
        tax_rate="0.13",
        invoice_type_code="general",
        config_version="tax-config-v1",
    )
    with pytest.raises(BusinessError):
        with app.state.session_factory.begin() as db:
            idempotency.execute(
                db,
                user_id,
                "tax.create",
                "rollback",
                body.model_dump(),
                lambda: tax.calculate(db, user_id, body),
            )
            raise BusinessError("ABORT", "abort outer transaction")
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(TaxCalculation)) == 0
        assert db.scalar(select(func.count()).select_from(IdempotencyRecord)) == 0


def test_parallel_repeated_creation(client, auth, app):
    body = {
        "gross_amount": "113.00",
        "tax_rate": "0.13",
        "invoice_type_code": "general",
        "config_version": "tax-config-v1",
    }

    def submit(_):
        return client.post(
            "/api/v1/tax-calculations", headers={**auth, "Idempotency-Key": "parallel"}, json=body
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(submit, range(4)))
    assert all(r.status_code in (201, 409, 503) for r in responses)
    retry = submit(None)
    assert retry.status_code == 201, retry.text
    ids = {r.json()["data"]["id"] for r in responses if r.status_code == 201}
    ids.add(retry.json()["data"]["id"])
    assert len(ids) == 1
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(TaxCalculation)) == 1
        assert db.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1
