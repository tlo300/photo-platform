"""Integration tests for GET /assets/{id} (issue #25).

Covers:
  1. Happy path — returns full detail (presigned URL, metadata, location, tags)
  2. Asset without metadata/location/tags — optional fields are null/empty
  3. 404 for a non-existent asset ID
  4. RLS isolation — cannot fetch another user's asset (returns 404)
  5. Unauthenticated request → 401

Requires the test PostgreSQL container from docker-compose.test.yml.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_asset_detail.py -v
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
    admin_email = f"admin-detail-{uuid.uuid4().hex[:8]}@test.com"
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
async def user_token(migrator_engine, admin_token: str) -> str:
    email = f"user-detail-{uuid.uuid4().hex[:8]}@test.com"
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


@pytest.fixture(scope="module")
async def other_user_token(migrator_engine, admin_token: str) -> str:
    email = f"user2-detail-{uuid.uuid4().hex[:8]}@test.com"
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
# Test-data helpers
# ---------------------------------------------------------------------------


def _insert_full_asset(engine, owner_id: str) -> str:
    """Insert an asset with metadata, GPS location, and a tag. Returns asset UUID."""
    asset_id = str(uuid.uuid4())
    storage_key = f"{owner_id}/{asset_id}/original.jpg"
    checksum = hashlib.sha256(asset_id.encode()).hexdigest()

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO media_assets"
                " (id, owner_id, original_filename, mime_type, storage_key,"
                "  file_size_bytes, checksum, captured_at, thumbnail_ready)"
                " VALUES (:id, :owner_id, :fn, :mime, :key, :size, :chk, :cat, true)"
            ),
            {
                "id": asset_id,
                "owner_id": owner_id,
                "fn": "photo.jpg",
                "mime": "image/jpeg",
                "key": storage_key,
                "size": 2048,
                "chk": checksum,
                "cat": datetime(2022, 6, 15, 10, 30, 0, tzinfo=timezone.utc),
            },
        )
        conn.execute(
            text(
                "INSERT INTO media_metadata (asset_id, make, model, width_px, height_px)"
                " VALUES (:a, :make, :model, :w, :h)"
            ),
            {"a": asset_id, "make": "Apple", "model": "iPhone 13", "w": 4032, "h": 3024},
        )
        # PostGIS point: longitude=4.9, latitude=52.37 (Amsterdam)
        conn.execute(
            text(
                "INSERT INTO locations (asset_id, point)"
                " VALUES (:a, ST_SetSRID(ST_MakePoint(4.9, 52.37), 4326))"
            ),
            {"a": asset_id},
        )
        # Tag
        conn.execute(
            text(
                "INSERT INTO tags (owner_id, name) VALUES (:o, :n)"
                " ON CONFLICT (owner_id, name) DO NOTHING"
            ),
            {"o": owner_id, "n": "Holiday"},
        )
        row = conn.execute(
            text("SELECT id FROM tags WHERE owner_id = :o AND name = :n"),
            {"o": owner_id, "n": "Holiday"},
        ).fetchone()
        conn.execute(
            text(
                "INSERT INTO asset_tags (asset_id, tag_id, source)"
                " VALUES (:a, :t, 'google')"
                " ON CONFLICT DO NOTHING"
            ),
            {"a": asset_id, "t": str(row[0])},
        )

    return asset_id


def _insert_bare_asset(engine, owner_id: str) -> str:
    """Insert an asset with no metadata, location, or tags. Returns asset UUID."""
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
                "fn": "bare.jpg",
                "mime": "image/jpeg",
                "key": storage_key,
                "size": 512,
                "chk": checksum,
            },
        )
    return asset_id


@pytest.fixture(scope="module")
def test_data(migrator_engine, user_token, other_user_token):
    with migrator_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id FROM users WHERE role = 'user'"
                " ORDER BY created_at DESC LIMIT 2"
            )
        ).fetchall()

    other_user_id = str(rows[0][0])
    user_id = str(rows[1][0])

    full_asset = _insert_full_asset(migrator_engine, user_id)
    bare_asset = _insert_bare_asset(migrator_engine, user_id)
    other_asset = _insert_bare_asset(migrator_engine, other_user_id)

    return {
        "user_id": user_id,
        "other_user_id": other_user_id,
        "full_asset": full_asset,
        "bare_asset": bare_asset,
        "other_asset": other_asset,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_full_asset(user_token, test_data):
    """Full asset returns all fields: URL, metadata, location, tags."""
    asset_id = test_data["full_asset"]
    with patch("app.api.assets.storage_service.generate_presigned_url", return_value="https://example.com/full.jpg"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                f"/assets/{asset_id}",
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["id"] == asset_id
    assert data["original_filename"] == "photo.jpg"
    assert data["mime_type"] == "image/jpeg"
    assert data["captured_at"] is not None
    assert data["full_url"] == "https://example.com/full.jpg"

    assert data["metadata"] is not None
    assert data["metadata"]["make"] == "Apple"
    assert data["metadata"]["model"] == "iPhone 13"
    assert data["metadata"]["width_px"] == 4032
    assert data["metadata"]["height_px"] == 3024

    assert data["location"] is not None
    assert abs(data["location"]["latitude"] - 52.37) < 0.001
    assert abs(data["location"]["longitude"] - 4.9) < 0.001

    assert len(data["tags"]) == 1
    assert data["tags"][0]["name"] == "Holiday"
    assert data["tags"][0]["source"] == "google"


@pytest.mark.asyncio
async def test_bare_asset_optional_fields_are_null(user_token, test_data):
    """Asset without metadata/location/tags returns null/empty for optional fields."""
    asset_id = test_data["bare_asset"]
    with patch("app.api.assets.storage_service.generate_presigned_url", return_value="https://example.com/bare.jpg"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                f"/assets/{asset_id}",
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["metadata"] is None
    assert data["location"] is None
    assert data["tags"] == []
    assert data["thumbnail_url"] is None


@pytest.mark.asyncio
async def test_not_found_returns_404(user_token):
    """Non-existent asset ID → 404."""
    missing_id = str(uuid.uuid4())
    with patch("app.api.assets.storage_service.generate_presigned_url", return_value="https://example.com/x.jpg"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                f"/assets/{missing_id}",
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rls_isolation(user_token, test_data):
    """User cannot fetch another user's asset — returns 404."""
    other_asset_id = test_data["other_asset"]
    with patch("app.api.assets.storage_service.generate_presigned_url", return_value="https://example.com/x.jpg"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                f"/assets/{other_asset_id}",
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_unauthenticated_returns_401():
    """No token → 401."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/assets/{uuid.uuid4()}")
    assert resp.status_code == 401
