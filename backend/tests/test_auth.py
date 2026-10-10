import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.security import decode


def test_login_refresh_logout(client, app):
    first = client.post("/api/v1/auth/dev-login", json={"account": "alice"}).json()["data"]
    auth = {"Authorization": "Bearer " + first["access_token"]}
    assert client.get("/api/v1/users/me", headers=auth).status_code == 200
    second = client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert second.status_code == 200
    assert (
        client.post(
            "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
        ).status_code
        == 401
    )
    token = second.json()["data"]["access_token"]
    claims = decode(app.state.settings, token, "access")
    assert claims["sub"] == first["user"]["id"]
    assert (
        client.post(
            "/api/v1/auth/logout", headers={"Authorization": "Bearer " + token}, json={}
        ).status_code
        == 200
    )
    assert client.get("/api/v1/users/me", headers=auth).status_code == 401
    assert (
        client.post(
            "/api/v1/auth/refresh", json={"refresh_token": second.json()["data"]["refresh_token"]}
        ).status_code
        == 401
    )


def test_wechat_login_uses_gateway(client):
    first = client.post("/api/v1/auth/wechat-login", json={"code": "alice"})
    second = client.post("/api/v1/auth/wechat-login", json={"code": "alice"})
    assert first.status_code == second.status_code == 200
    assert first.json()["data"]["user"]["id"] == second.json()["data"]["user"]["id"]
    assert "openid" not in first.json()["data"]["user"]


def test_dev_login_disabled_by_default(client, app):
    app.state.settings.enable_dev_login = False
    assert client.post("/api/v1/auth/dev-login", json={"account": "alice"}).status_code == 403


def test_production_rejects_development_config():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            auth_signing_key="x" * 40,
            environment="production",
            enable_dev_login=True,
        )


def test_profile_version_conflict(client, auth):
    changed = client.patch(
        "/api/v1/users/me", headers=auth, json={"display_name": "新名称", "expected_version": 1}
    )
    assert changed.status_code == 200 and changed.json()["data"]["version"] == 2
    stale = client.patch(
        "/api/v1/users/me", headers=auth, json={"display_name": "旧名称", "expected_version": 1}
    )
    assert stale.status_code == 409 and stale.json()["code"] == "VERSION_CONFLICT"
    assert client.get("/api/v1/users/me", headers=auth).json()["data"]["display_name"] == "新名称"


def test_shared_settings_version(client, auth):
    result = client.patch(
        "/api/v1/users/me/notification-settings",
        headers=auth,
        json={"in_app_task_updates": False, "expected_version": 1},
    )
    assert result.status_code == 200 and result.json()["data"]["version"] == 2
    assert (
        client.patch(
            "/api/v1/users/me/privacy-settings",
            headers=auth,
            json={"retain_chat_history": False, "expected_version": 1},
        ).status_code
        == 409
    )


def test_auth_required_and_no_sensitive_validation_echo(client):
    assert client.get("/api/v1/users/me").status_code == 401
    result = client.post("/api/v1/auth/refresh", json={"refresh_token": "sensitive"})
    assert result.status_code == 422
    assert "sensitive" not in result.text
    assert result.headers["x-request-id"] == result.json()["request_id"]
