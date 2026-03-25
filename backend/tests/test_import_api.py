"""Integration tests for the Takeout import API (issue #20).

Covers:
  1. POST /import/takeout — valid zip → 202 + job_id returned
  2. POST /import/takeout — oversized zip → 413
  3. POST /import/takeout — non-zip file → 400
  4. POST /import/takeout — unauthenticated → 401
  5. GET /import/jobs/{job_id} — owner can fetch their job progress
  6. GET /import/jobs/{job_id} — unknown job_id → 404
  7. GET /import/jobs/{job_id} — another user's job → 404 (RLS isolation)
  8. GET /import/jobs/{job_id} — unauthenticated → 401

Celery tasks are mocked so no Redis is required for these tests.
Requires the test PostgreSQL container from docker-compose.test.yml.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_import_api.py -v
"""

import io
import os
import uuid
import zipfile
from unittest.mock import patch, MagicMock

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

IMPORT_URL = "/import/takeout"
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
# User helpers
# ---------------------------------------------------------------------------


def _make_invitation(engine, email: str, admin_id: str) -> str:
    """Insert an invitation row and return the raw token."""
    import secrets
    import hashlib
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
    """Register an admin user and return their JWT."""
    admin_email = f"admin-import-{uuid.uuid4().hex[:8]}@test.com"
    password = "AdminP@ss1!"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            REGISTER_URL,
            json={"email": admin_email, "display_name": "Admin", "password": password},
        )
        assert resp.status_code == 201, resp.text

    # Promote to admin
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
    """Register a regular user and return their JWT."""
    email = f"user-import-{uuid.uuid4().hex[:8]}@test.com"
    password = "UserP@ss1!"

    # Get admin ID
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


@pytest.fixture
async def second_user_token(migrator_engine, admin_token: str) -> str:
    """Register a second regular user and return their JWT."""
    email = f"user2-import-{uuid.uuid4().hex[:8]}@test.com"
    password = "UserP@ss2!"

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
                "display_name": "User2",
                "password": password,
                "invitation_token": invitation_token,
            },
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Zip helpers
# ---------------------------------------------------------------------------


def _make_zip(entries: dict[str, bytes]) -> bytes:
    """Return zip bytes containing the given {name: content} entries."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


_TINY_ZIP = _make_zip({"photos/dummy.txt": b"not-a-media-file"})


# ---------------------------------------------------------------------------
# Tests — POST /import/takeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_import_returns_job_id(user_token: str):
    """Valid zip upload returns 202 with a job_id."""
    with (
        patch("app.api.import_.storage_service._client") as mock_s3,
        patch("app.api.import_.process_takeout_zip") as mock_task,
    ):
        mock_s3.upload_fileobj.return_value = None
        mock_task.delay.return_value = MagicMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                IMPORT_URL,
                files={"file": ("archive.zip", _TINY_ZIP, "application/zip")},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert "job_id" in body
    uuid.UUID(body["job_id"])  # must be valid UUID


@pytest.mark.asyncio
async def test_start_import_unauthenticated():
    """No token → 401."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            IMPORT_URL,
            files={"file": ("archive.zip", _TINY_ZIP, "application/zip")},
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_start_import_oversized(user_token: str):
    """Upload that exceeds MAX_UPLOAD_SIZE_BYTES → 413."""
    with patch("app.api.import_.settings") as mock_settings:
        mock_settings.max_upload_size_bytes = 10  # force tiny limit

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                IMPORT_URL,
                files={"file": ("archive.zip", _TINY_ZIP, "application/zip")},
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_start_import_non_zip_content(user_token: str):
    """Upload with non-zip magic bytes → 400."""
    not_a_zip = b"This is plaintext, not a zip archive." + b"\x00" * 100

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            IMPORT_URL,
            files={"file": ("notzip.bin", not_a_zip, "application/zip")},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Tests — GET /import/jobs/{job_id}
# ---------------------------------------------------------------------------


@pytest.fixture
async def created_job_id(user_token: str, migrator_engine) -> str:
    """Create a job via the API and return its job_id."""
    with (
        patch("app.api.import_.storage_service._client") as mock_s3,
        patch("app.api.import_.process_takeout_zip") as mock_task,
    ):
        mock_s3.upload_fileobj.return_value = None
        mock_task.delay.return_value = MagicMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                IMPORT_URL,
                files={"file": ("archive.zip", _TINY_ZIP, "application/zip")},
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 202
    return resp.json()["job_id"]


@pytest.mark.asyncio
async def test_get_job_happy_path(user_token: str, created_job_id: str):
    """Owner can fetch their job and gets all expected fields."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/import/jobs/{created_job_id}",
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == created_job_id
    assert body["status"] in ("pending", "processing", "done", "failed")
    assert body["processed"] == 0
    assert body["duplicates"] == 0
    assert body["errors"] == []
    assert "total" in body


@pytest.mark.asyncio
async def test_get_job_unknown_id(user_token: str):
    """Non-existent job_id → 404."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/import/jobs/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_job_other_user_cannot_see(second_user_token: str, created_job_id: str):
    """A different authenticated user gets 404 — RLS isolates jobs by owner."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/import/jobs/{created_job_id}",
            headers={"Authorization": f"Bearer {second_user_token}"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_job_unauthenticated(created_job_id: str):
    """No token → 401."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/import/jobs/{created_job_id}")
    assert resp.status_code == 401
