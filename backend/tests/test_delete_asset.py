"""Integration tests for DELETE /assets/{id} (issue #175).

Covers:
  1. Happy path — asset is deleted and subsequent GET returns 404
  2. 404 for a non-existent asset ID
  3. RLS isolation — cannot delete another user's asset (returns 404)
  4. Unauthenticated request → 401

Requires the test PostgreSQL container from docker-compose.test.yml.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_delete_asset.py -v
"""

import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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
    admin_email = f"admin-delete-{uuid.uuid4().hex[:8]}@test.com"
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


@pytest.fixture(scope="module")
async def user_token(migrator_engine, admin_token: str) -> tuple[str, str]:
    """Returns (token, user_id)."""
    email = f"user-delete-{uuid.uuid4().hex[:8]}@test.com"
    password = "UserP@ss1!"

    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM users WHERE email LIKE 'admin-delete-%@test.com' LIMIT 1")
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
        token = resp.json()["access_token"]

    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM users WHERE email = :e"),
            {"e": email},
        ).fetchone()
        user_id = str(row[0])

    return token, user_id


@pytest.fixture(scope="module")
async def other_user_token(migrator_engine, admin_token: str) -> tuple[str, str]:
    """Returns (token, user_id) for a second user."""
    email = f"user2-delete-{uuid.uuid4().hex[:8]}@test.com"
    password = "UserP@ss2!"

    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM users WHERE email LIKE 'admin-delete-%@test.com' LIMIT 1")
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
        token = resp.json()["access_token"]

    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM users WHERE email = :e"),
            {"e": email},
        ).fetchone()
        user_id = str(row[0])

    return token, user_id


# ---------------------------------------------------------------------------
# Asset helpers
# ---------------------------------------------------------------------------


def _insert_asset(engine, owner_id: str) -> str:
    """Insert a minimal asset and return its UUID string."""
    asset_id = str(uuid.uuid4())
    storage_key = f"{owner_id}/{asset_id}/original.jpg"
    checksum = hashlib.sha256(asset_id.encode()).hexdigest()

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO media_assets"
                " (id, owner_id, original_filename, mime_type, storage_key,"
                "  file_size_bytes, checksum)"
                " VALUES (:id, :owner_id, :fn, :mime, :key, :size, :chk)"
            ),
            {
                "id": asset_id,
                "owner_id": owner_id,
                "fn": "photo.jpg",
                "mime": "image/jpeg",
                "key": storage_key,
                "size": 1024,
                "chk": checksum,
            },
        )
    return asset_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_delete_asset_happy_path(migrator_engine, user_token):
    """DELETE /assets/{id} returns 204 and the asset is no longer fetchable."""
    token, user_id = user_token
    asset_id = _insert_asset(migrator_engine, user_id)

    with patch("app.api.assets.storage_service") as mock_storage:
        mock_storage.delete.return_value = None

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete(
                f"/assets/{asset_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 204

        # Verify the asset is gone
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            get_resp = await client.get(
                f"/assets/{asset_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert get_resp.status_code == 404

        # All expected storage keys were deleted
        deleted_keys = [call.args[0] for call in mock_storage.delete.call_args_list]
        assert any(asset_id in k for k in deleted_keys), "Original storage key not deleted"


@pytest.mark.anyio
async def test_delete_asset_not_found(user_token):
    """DELETE /assets/{id} returns 404 for a non-existent asset."""
    token, _ = user_token
    fake_id = str(uuid.uuid4())

    with patch("app.api.assets.storage_service"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete(
                f"/assets/{fake_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_delete_asset_rls_isolation(migrator_engine, user_token, other_user_token):
    """DELETE /assets/{id} returns 404 when the asset belongs to a different user."""
    token, user_id = user_token
    other_token, _ = other_user_token
    asset_id = _insert_asset(migrator_engine, user_id)

    with patch("app.api.assets.storage_service"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.delete(
                f"/assets/{asset_id}",
                headers={"Authorization": f"Bearer {other_token}"},
            )
    assert resp.status_code == 404

    # Asset should still exist for the owner
    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM media_assets WHERE id = :a"),
            {"a": asset_id},
        ).fetchone()
    assert row is not None, "Asset was deleted by wrong user"


@pytest.mark.anyio
async def test_delete_asset_unauthenticated(migrator_engine, user_token):
    """DELETE /assets/{id} returns 401 when no token is provided."""
    _, user_id = user_token
    asset_id = _insert_asset(migrator_engine, user_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.delete(f"/assets/{asset_id}")
    assert resp.status_code == 401
