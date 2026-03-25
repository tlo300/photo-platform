"""Integration tests for the folder import API endpoint (issue #74).

Covers:
  1. POST /import/folder — valid directory → 202 + job_id returned
  2. POST /import/folder — path outside import_base_dir → 400
  3. POST /import/folder — path does not exist → 400
  4. POST /import/folder — path is a file, not a directory → 400
  5. POST /import/folder — unauthenticated → 401
  6. GET /import/jobs/{job_id} — folder job progress readable by owner

Celery tasks are mocked so no Redis is required.
Requires the test PostgreSQL container from docker-compose.test.yml.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_import_folder_api.py -v
"""

import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text

from app.main import app

MIGRATOR_URL = os.environ.get(
    "TEST_DATABASE_MIGRATOR_URL",
    "postgresql+psycopg://migrator:testpassword@localhost:5433/photo_test",
)

FOLDER_URL = "/import/folder"
REGISTER_URL = "/auth/register"
LOGIN_URL = "/auth/login"


# ---------------------------------------------------------------------------
# Schema fixtures
# ---------------------------------------------------------------------------


def _alembic_cfg() -> Config:
    cfg = Config()
    ini_path = os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
    cfg.config_file_name = os.path.abspath(ini_path)
    cfg.set_main_option("sqlalchemy.url", MIGRATOR_URL)
    migrations_path = os.path.join(os.path.dirname(__file__), "..", "migrations")
    cfg.set_main_option("script_location", os.path.abspath(migrations_path))
    return cfg


@pytest.fixture(scope="module", autouse=True)
def run_migrations():
    cfg = _alembic_cfg()
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


@pytest.fixture(scope="module")
def migrator_engine():
    e = create_engine(MIGRATOR_URL)
    yield e
    e.dispose()


# ---------------------------------------------------------------------------
# User helpers (mirrors test_import_api.py)
# ---------------------------------------------------------------------------


def _make_invitation(engine, email: str, admin_id: str) -> str:
    import hashlib
    import secrets
    from datetime import datetime, timedelta, timezone

    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invitations (email, token_hash, created_by, expires_at)"
                " VALUES (:email, :hash, :created_by, :expires_at)"
            ),
            {"email": email, "hash": token_hash, "created_by": admin_id, "expires_at": expires_at},
        )
    return raw


@pytest.fixture(scope="module")
async def admin_token(migrator_engine) -> str:
    admin_email = f"admin-folder-{uuid.uuid4().hex[:8]}@test.com"
    password = "AdminP@ss1!"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            REGISTER_URL,
            json={"email": admin_email, "display_name": "Admin", "password": password},
        )
        assert resp.status_code == 201, resp.text

    with migrator_engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET role = 'admin' WHERE email = :e"),
            {"e": admin_email},
        )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(LOGIN_URL, json={"email": admin_email, "password": password})
        assert resp.status_code == 200
        return resp.json()["access_token"]


@pytest.fixture
async def user_token(migrator_engine, admin_token: str) -> str:
    email = f"user-folder-{uuid.uuid4().hex[:8]}@test.com"
    password = "UserP@ss1!"

    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM users WHERE role = 'admin' ORDER BY created_at LIMIT 1")
        ).fetchone()
        admin_id = str(row[0])

    invitation_token = _make_invitation(migrator_engine, email, admin_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            REGISTER_URL,
            json={
                "email": email,
                "display_name": "User",
                "password": password,
                "invitation_token": invitation_token,
            },
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Tests — POST /import/folder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_folder_import_returns_job_id(user_token: str, tmp_path: Path):
    """Valid directory inside import_base_dir → 202 with job_id."""
    with (
        patch("app.api.import_.settings") as mock_settings,
        patch("app.api.import_.process_takeout_folder") as mock_task,
    ):
        mock_settings.import_base_dir = str(tmp_path)
        mock_task.delay.return_value = MagicMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                FOLDER_URL,
                json={"folder_path": str(tmp_path)},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert "job_id" in body
    uuid.UUID(body["job_id"])  # must be valid UUID


@pytest.mark.asyncio
async def test_folder_import_outside_base_dir_rejected(user_token: str, tmp_path: Path):
    """Path outside import_base_dir → 400."""
    import tempfile

    with patch("app.api.import_.settings") as mock_settings:
        # Set base_dir to a subdirectory of tmp_path; submit tmp_path itself (parent)
        base = tmp_path / "import"
        base.mkdir()
        mock_settings.import_base_dir = str(base)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                FOLDER_URL,
                json={"folder_path": str(tmp_path)},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 400
    assert "import base directory" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_folder_import_nonexistent_path_rejected(user_token: str, tmp_path: Path):
    """Path that does not exist → 400."""
    missing = tmp_path / "does-not-exist"

    with patch("app.api.import_.settings") as mock_settings:
        mock_settings.import_base_dir = str(tmp_path)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                FOLDER_URL,
                json={"folder_path": str(missing)},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 400
    assert "does not exist" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_folder_import_file_path_rejected(user_token: str, tmp_path: Path):
    """Path pointing to a file (not a directory) → 400."""
    a_file = tmp_path / "archive.zip"
    a_file.write_bytes(b"PK\x03\x04")

    with patch("app.api.import_.settings") as mock_settings:
        mock_settings.import_base_dir = str(tmp_path)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                FOLDER_URL,
                json={"folder_path": str(a_file)},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 400
    assert "does not exist or is not a directory" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_folder_import_unauthenticated(tmp_path: Path):
    """No token → 401."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            FOLDER_URL,
            json={"folder_path": str(tmp_path)},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_folder_import_job_progress_readable(user_token: str, tmp_path: Path):
    """Folder job appears in GET /import/jobs/{job_id} with expected shape."""
    with (
        patch("app.api.import_.settings") as mock_settings,
        patch("app.api.import_.process_takeout_folder") as mock_task,
    ):
        mock_settings.import_base_dir = str(tmp_path)
        mock_task.delay.return_value = MagicMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            create_resp = await client.post(
                FOLDER_URL,
                json={"folder_path": str(tmp_path)},
                headers={"Authorization": f"Bearer {user_token}"},
            )
        assert create_resp.status_code == 202
        job_id = create_resp.json()["job_id"]

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                f"/import/jobs/{job_id}",
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == job_id
    assert body["status"] in ("pending", "processing", "done", "failed")
    assert body["processed"] == 0
    assert body["duplicates"] == 0
    assert body["errors"] == []
