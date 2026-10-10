from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text


def test_migration_roundtrip(app, client):
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    # Fixture is a disposable SQLite database without user business data.
    command.downgrade(config, "base")
    assert "users" not in inspect(app.state.engine).get_table_names()
    command.upgrade(config, "head")
    assert client.get("/health/ready").status_code == 200
    assert {"users", "files", "jobs", "tax_calculations"}.issubset(
        inspect(app.state.engine).get_table_names()
    )


def test_readiness_rejects_missing_migration_version(app, client):
    with app.state.session_factory.begin() as db:
        db.execute(text("UPDATE alembic_version SET version_num='wrong-version'"))
    assert client.get("/health/ready").status_code == 503


def test_stage_two_migration_preserves_existing_upload_data(app, client, auth):
    from app.db.models import UploadSession

    response = client.post(
        "/api/v1/upload-sessions",
        headers={**auth, "Idempotency-Key": "preserve"},
        json={
            "filename": "existing.pdf",
            "content_type": "application/pdf",
            "purpose": "invoice",
            "size_bytes": "123",
        },
    )
    id = response.json()["data"]["id"]
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    command.downgrade(config, "ef64051eb7ee")
    assert client.get("/health/ready").status_code == 503
    command.upgrade(config, "head")
    with app.state.session_factory() as db:
        row = db.get(UploadSession, id)
        assert (
            row.filename == "existing.pdf"
            and row.storage_backend == "local"
            and int(row.size_bytes) == 123
        )
    assert client.get("/health/ready").status_code == 200
