"""Integration tests for album deletion (issue #183).

Covers:
  1. GET /albums/{id} returns exclusive_asset_count = 0 for empty album
  2. GET /albums/{id} returns exclusive_asset_count = N for exclusive assets
  3. GET /albums/{id} returns exclusive_asset_count = 0 when all assets shared
  4. DELETE /albums/{id} (default) deletes album, assets remain
  5. DELETE /albums/{id}?delete_exclusive_assets=true deletes album + exclusive assets
  6. DELETE /albums/{id}?delete_exclusive_assets=true leaves shared assets untouched
  7. DELETE /albums/{id} non-existent album → 404
  8. DELETE /albums/{id} RLS: cannot delete another user's album → 404
  9. DELETE /albums/{id} unauthenticated → 401

Requires the test PostgreSQL container from docker-compose.test.yml.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_delete_album.py -v
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
    email = f"admin-del-album-{uuid.uuid4().hex[:8]}@test.com"
    password = "AdminP@ss1!"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(REGISTER_URL, json={"email": email, "display_name": "Admin", "password": password})
        assert resp.status_code == 201, resp.text
    with migrator_engine.begin() as conn:
        conn.execute(text("UPDATE users SET role = 'admin' WHERE email = :e"), {"e": email})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(LOGIN_URL, json={"email": email, "password": password})
        assert resp.status_code == 200
        return resp.json()["access_token"]


@pytest.fixture(scope="module")
async def user_token(migrator_engine, admin_token: str) -> str:
    email = f"user-del-album-{uuid.uuid4().hex[:8]}@test.com"
    password = "UserP@ss1!"
    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM users WHERE email LIKE '%admin-del-album%' ORDER BY created_at DESC LIMIT 1")
        ).fetchone()
        admin_id = str(row[0])
    inv = _make_invitation(migrator_engine, email, admin_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            REGISTER_URL,
            json={"email": email, "display_name": "User", "password": password, "invitation_token": inv},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["access_token"]


@pytest.fixture(scope="module")
async def other_user_token(migrator_engine, admin_token: str) -> str:
    email = f"user2-del-album-{uuid.uuid4().hex[:8]}@test.com"
    password = "UserP@ss2!"
    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM users WHERE email LIKE '%admin-del-album%' ORDER BY created_at DESC LIMIT 1")
        ).fetchone()
        admin_id = str(row[0])
    inv = _make_invitation(migrator_engine, email, admin_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            REGISTER_URL,
            json={"email": email, "display_name": "User2", "password": password, "invitation_token": inv},
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["access_token"]


def _get_user_id(engine, email_fragment: str) -> str:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM users WHERE email LIKE :f ORDER BY created_at DESC LIMIT 1"),
            {"f": f"%{email_fragment}%"},
        ).fetchone()
        return str(row[0])


def _insert_asset(engine, owner_id: str) -> str:
    asset_id = str(uuid.uuid4())
    storage_key = f"{owner_id}/{asset_id}/original.jpg"
    checksum = hashlib.sha256(asset_id.encode()).hexdigest()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO media_assets"
                " (id, owner_id, original_filename, mime_type, storage_key,"
                "  file_size_bytes, checksum, thumbnail_ready)"
                " VALUES (:id, :owner_id, :fn, :mime, :key, :size, :chk, :tr)"
            ),
            {
                "id": asset_id, "owner_id": owner_id, "fn": "photo.jpg",
                "mime": "image/jpeg", "key": storage_key, "size": 1024,
                "chk": checksum, "tr": False,
            },
        )
    return asset_id


def _link_asset(engine, album_id: str, asset_id: str, sort_order: int = 0) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO album_assets (album_id, asset_id, sort_order)"
                " VALUES (:album_id, :asset_id, :sort_order)"
            ),
            {"album_id": album_id, "asset_id": asset_id, "sort_order": sort_order},
        )


@pytest.mark.asyncio
async def test_exclusive_asset_count_empty_album(user_token):
    """GET /albums/{id} returns exclusive_asset_count=0 for an empty album."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/albums", json={"title": "Empty Album"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 201
        album_id = resp.json()["id"]
        resp = await client.get(
            f"/albums/{album_id}",
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 200
    assert resp.json()["exclusive_asset_count"] == 0


@pytest.mark.asyncio
async def test_exclusive_asset_count_all_exclusive(user_token, migrator_engine):
    """GET /albums/{id} returns exclusive_asset_count=2 when both assets are exclusive."""
    user_id = _get_user_id(migrator_engine, "user-del-album")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/albums", json={"title": "Exclusive Album"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 201
        album_id = resp.json()["id"]
    asset1 = _insert_asset(migrator_engine, user_id)
    asset2 = _insert_asset(migrator_engine, user_id)
    _link_asset(migrator_engine, album_id, asset1)
    _link_asset(migrator_engine, album_id, asset2, sort_order=1)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/albums/{album_id}",
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 200
    assert resp.json()["exclusive_asset_count"] == 2


@pytest.mark.asyncio
async def test_exclusive_asset_count_shared_asset(user_token, migrator_engine):
    """GET /albums/{id} returns exclusive_asset_count=0 when all assets are in another album too."""
    user_id = _get_user_id(migrator_engine, "user-del-album")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/albums", json={"title": "Album A"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 201
        album_a = resp.json()["id"]
        resp = await client.post(
            "/albums", json={"title": "Album B"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 201
        album_b = resp.json()["id"]
    shared_asset = _insert_asset(migrator_engine, user_id)
    _link_asset(migrator_engine, album_a, shared_asset)
    _link_asset(migrator_engine, album_b, shared_asset)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/albums/{album_a}",
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 200
    assert resp.json()["exclusive_asset_count"] == 0
