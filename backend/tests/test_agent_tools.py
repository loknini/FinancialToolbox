import pytest
from sqlalchemy import func, select

from app.agent.tools import ToolContext, build_registry
from app.core.errors import BusinessError
from app.db.models import TaxCalculation


def test_tool_calculation_uses_same_service_and_idempotency(app, client, auth):
    user_id = client.get("/api/v1/users/me", headers=auth).json()["data"]["id"]
    registry = build_registry()
    args = {
        "gross_amount": "113.00",
        "tax_rate": "0.13",
        "invoice_type_code": "general",
        "config_version": "tax-config-v1",
    }
    with app.state.session_factory.begin() as db:
        ctx = ToolContext(db=db, user_id=user_id, operation_key="run:one:step:one")
        first = registry.execute("calculate_tax", args, ctx)
        second = registry.execute("calculate_tax", args, ctx)
        assert first == second and first["tax_amount"] == "13.00"
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(TaxCalculation)) == 1


def test_tools_reject_unknown_tool_and_user_id(app, client, auth):
    user_id = client.get("/api/v1/users/me", headers=auth).json()["data"]["id"]
    registry = build_registry()
    with app.state.session_factory.begin() as db:
        ctx = ToolContext(db=db, user_id=user_id, operation_key="run:one:step:one")
        with pytest.raises(BusinessError) as error:
            registry.execute("run_sql", {"sql": "SELECT * FROM users"}, ctx)
        assert error.value.code == "TOOL_NOT_ALLOWED"
        with pytest.raises(BusinessError):
            registry.execute("get_tax_config", {"user_id": "other"}, ctx)
    definitions = registry.definitions()
    assert all(d["input_schema"]["additionalProperties"] is False for d in definitions)
