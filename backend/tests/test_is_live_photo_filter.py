"""Integration tests for is_live_photo filter on GET /assets.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_is_live_photo_filter.py -v
"""

import hashlib
import os
import secrets
import uuid
from datetime import datetime, timezone

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

ASSETS_URL = "/assets"
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
    expires_at = datetime(2099, 1, 1, tzinfo=timezone.utc)
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
async def user_token(migrator_engine) -> tuple[str, str]:
    """Returns (token, email) for the registered test user."""
    admin_email = f"admin-lp-{uuid.uuid4().hex[:8]}@test.com"
    password = "AdminP@ss1!"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(REGISTER_URL, json={"email": admin_email, "display_name": "Admin", "password": password})
        assert resp.status_code == 201, resp.text
    with migrator_engine.begin() as conn:
        conn.execute(text("UPDATE users SET role = 'admin' WHERE email = :e"), {"e": admin_email})
    user_email = f"user-lp-{uuid.uuid4().hex[:8]}@test.com"
    with migrator_engine.connect() as conn:
        row = conn.execute(text("SELECT id FROM users WHERE email = :e"), {"e": admin_email}).fetchone()
        admin_id = str(row[0])
    invitation = _make_invitation(migrator_engine, user_email, admin_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(REGISTER_URL, json={"email": user_email, "display_name": "User", "password": "UserP@ss1!", "invitation_token": invitation})
        assert resp.status_code == 201, resp.text
        return resp.json()["access_token"], user_email


def _insert_asset(engine, owner_id: str, is_live: bool) -> str:
    """Insert a minimal asset row directly. Returns asset id."""
    asset_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO media_assets "
                "(id, owner_id, storage_key, original_filename, mime_type, file_size_bytes, "
                " checksum, thumbnail_ready, is_live_photo, captured_at) "
                "VALUES (:id, :owner_id, :key, :fname, :mime, 1000, :checksum, true, :live, NOW())"
            ),
            {
                "id": asset_id,
                "owner_id": owner_id,
                "key": f"{owner_id}/{asset_id}/original.jpg",
                "fname": f"test-{asset_id[:8]}.jpg",
                "mime": "image/jpeg",
                "checksum": uuid.uuid4().hex,
                "live": is_live,
            },
        )
    return asset_id


@pytest.fixture(scope="module")
def owner_id(migrator_engine, user_token) -> str:
    """Get the user_id for the user_token fixture by email."""
    token, email = user_token
    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM users WHERE email = :e"), {"e": email}
        ).fetchone()
        return str(row[0])


@pytest.fixture(scope="module")
def test_data(migrator_engine, owner_id):
    live_id = _insert_asset(migrator_engine, owner_id, is_live=True)
    still_id = _insert_asset(migrator_engine, owner_id, is_live=False)
    return {"live_id": live_id, "still_id": still_id}


@pytest.mark.asyncio
async def test_is_live_photo_true_returns_only_live(user_token, test_data):
    token, _ = user_token
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            ASSETS_URL,
            params={"is_live_photo": "true"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    ids = [a["id"] for a in resp.json()["items"]]
    assert test_data["live_id"] in ids
    assert test_data["still_id"] not in ids


@pytest.mark.asyncio
async def test_is_live_photo_false_excludes_live(user_token, test_data):
    token, _ = user_token
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            ASSETS_URL,
            params={"is_live_photo": "false"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    ids = [a["id"] for a in resp.json()["items"]]
    assert test_data["still_id"] in ids
    assert test_data["live_id"] not in ids


@pytest.mark.asyncio
async def test_no_live_filter_returns_both(user_token, test_data):
    token, _ = user_token
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            ASSETS_URL,
            params={"limit": "200"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    ids = [a["id"] for a in resp.json()["items"]]
    assert test_data["live_id"] in ids
    assert test_data["still_id"] in ids
