from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.jobs.worker import Worker
from app.main import create_app


class FakeWechat:
    def exchange(self, code):
        return "test-wechat-" + code


@pytest.fixture
def app(tmp_path, monkeypatch):
    database_url = "sqlite:///" + (tmp_path / "test.db").as_posix()
    key = "test-key-not-for-deployment-" + "x" * 32
    monkeypatch.setenv("FT_DATABASE_URL", database_url)
    monkeypatch.setenv("FT_AUTH_SIGNING_KEY", key)
    monkeypatch.setenv("FT_ENABLE_DEV_LOGIN", "true")
    monkeypatch.setenv("FT_ENVIRONMENT", "test")
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[1] / "migrations")
    )
    command.upgrade(config, "head")
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=database_url,
        auth_signing_key=key,
        enable_dev_login=True,
        storage_root=tmp_path / "storage",
        upload_part_bytes=1024,
        public_base_url="http://testserver",
    )
    return create_app(settings, wechat=FakeWechat())


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


@pytest.fixture
def worker(app):
    return Worker(app.state.settings, app.state.session_factory, app.state.storage, app.state.ocr)


@pytest.fixture
def auth(client):
    result = client.post("/api/v1/auth/dev-login", json={"account": "alice"})
    assert result.status_code == 200, result.text
    return {"Authorization": "Bearer " + result.json()["data"]["access_token"]}
