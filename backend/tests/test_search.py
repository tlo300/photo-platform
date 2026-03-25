"""Integration tests for GET /assets/search (issue #26).

Covers:
  1.  Match by description text
  2.  Match by tag name
  3.  Match by locality display_name
  4.  Match by country
  5.  Empty query returns assets (falls back to timeline order)
  6.  No match returns empty list
  7.  RLS isolation — user only sees their own assets
  8.  Unauthenticated request → 401

Requires the test PostgreSQL container from docker-compose.test.yml.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_search.py -v
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

SEARCH_URL = "/assets/search"
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
# User helpers (same pattern as other test modules)
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
    admin_email = f"admin-search-{uuid.uuid4().hex[:8]}@test.com"
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
    email = f"user-search-{uuid.uuid4().hex[:8]}@test.com"
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
    email = f"user2-search-{uuid.uuid4().hex[:8]}@test.com"
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
# Data helpers
# ---------------------------------------------------------------------------


def _insert_asset(
    engine,
    owner_id: str,
    *,
    description: str | None = None,
    tag: str | None = None,
    display_name: str | None = None,
    country: str | None = None,
    captured_at: datetime | None = None,
    make: str | None = None,
    model: str | None = None,
) -> str:
    """Insert an asset with optional description, tag, location, and camera metadata. Returns asset UUID."""
    asset_id = str(uuid.uuid4())
    storage_key = f"{owner_id}/{asset_id}/original.jpg"
    checksum = hashlib.sha256(asset_id.encode()).hexdigest()
    cat = captured_at or datetime(2023, 1, 1, tzinfo=timezone.utc)

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO media_assets"
                " (id, owner_id, original_filename, mime_type, storage_key,"
                "  file_size_bytes, checksum, description, captured_at)"
                " VALUES (:id, :owner_id, :fn, :mime, :key, :size, :chk, :desc, :cat)"
            ),
            {
                "id": asset_id,
                "owner_id": owner_id,
                "fn": "photo.jpg",
                "mime": "image/jpeg",
                "key": storage_key,
                "size": 1024,
                "chk": checksum,
                "desc": description,
                "cat": cat,
            },
        )

        if make or model:
            conn.execute(
                text(
                    "INSERT INTO media_metadata (asset_id, make, model)"
                    " VALUES (:asset_id, :make, :model)"
                ),
                {"asset_id": asset_id, "make": make, "model": model},
            )

        if tag:
            # Upsert tag then link it.
            conn.execute(
                text(
                    "INSERT INTO tags (id, owner_id, name)"
                    " VALUES (gen_random_uuid(), :owner_id, :name)"
                    " ON CONFLICT (owner_id, name) DO NOTHING"
                ),
                {"owner_id": owner_id, "name": tag},
            )
            row = conn.execute(
                text("SELECT id FROM tags WHERE owner_id = :owner_id AND name = :name"),
                {"owner_id": owner_id, "name": tag},
            ).fetchone()
            conn.execute(
                text(
                    "INSERT INTO asset_tags (asset_id, tag_id)"
                    " VALUES (:asset_id, :tag_id)"
                    " ON CONFLICT DO NOTHING"
                ),
                {"asset_id": asset_id, "tag_id": str(row[0])},
            )

        if display_name or country:
            conn.execute(
                text(
                    "INSERT INTO locations"
                    " (asset_id, point, display_name, country)"
                    " VALUES (:asset_id, ST_SetSRID(ST_MakePoint(4.9, 52.4), 4326),"
                    "         :display_name, :country)"
                ),
                {
                    "asset_id": asset_id,
                    "display_name": display_name,
                    "country": country,
                },
            )

    return asset_id


def _owner_id(engine, token: str) -> str:
    """Decode the user_id from a JWT token via the DB (simpler than jwt decode in tests)."""
    # We use the asset we're about to create — instead, look up the user from the token
    # by re-using the register flow. Easier: decode the JWT payload directly.
    import base64
    import json

    payload_b64 = token.split(".")[1]
    # Add padding
    payload_b64 += "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    return payload["sub"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_by_description(migrator_engine, user_token):
    owner_id = _owner_id(migrator_engine, user_token)
    asset_id = _insert_asset(migrator_engine, owner_id, description="A beautiful sunset over the mountains")

    with patch("app.services.storage.storage_service.generate_presigned_url", return_value="http://fake/thumb"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                SEARCH_URL,
                params={"q": "sunset mountains"},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 200
    data = resp.json()
    ids = [i["id"] for i in data["items"]]
    assert asset_id in ids


@pytest.mark.asyncio
async def test_search_by_tag(migrator_engine, user_token):
    owner_id = _owner_id(migrator_engine, user_token)
    asset_id = _insert_asset(migrator_engine, owner_id, tag="Amsterdam")

    with patch("app.services.storage.storage_service.generate_presigned_url", return_value="http://fake/thumb"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                SEARCH_URL,
                params={"q": "Amsterdam"},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 200
    ids = [i["id"] for i in resp.json()["items"]]
    assert asset_id in ids


@pytest.mark.asyncio
async def test_search_by_display_name(migrator_engine, user_token):
    owner_id = _owner_id(migrator_engine, user_token)
    asset_id = _insert_asset(migrator_engine, owner_id, display_name="Jordaan, Amsterdam")

    with patch("app.services.storage.storage_service.generate_presigned_url", return_value="http://fake/thumb"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                SEARCH_URL,
                params={"q": "Jordaan"},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 200
    ids = [i["id"] for i in resp.json()["items"]]
    assert asset_id in ids


@pytest.mark.asyncio
async def test_search_by_country(migrator_engine, user_token):
    owner_id = _owner_id(migrator_engine, user_token)
    asset_id = _insert_asset(migrator_engine, owner_id, country="Netherlands")

    with patch("app.services.storage.storage_service.generate_presigned_url", return_value="http://fake/thumb"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                SEARCH_URL,
                params={"q": "Netherlands"},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 200
    ids = [i["id"] for i in resp.json()["items"]]
    assert asset_id in ids


@pytest.mark.asyncio
async def test_empty_query_returns_assets(migrator_engine, user_token):
    """Empty q falls back to timeline — returns assets ordered by captured_at."""
    owner_id = _owner_id(migrator_engine, user_token)
    _insert_asset(migrator_engine, owner_id)  # ensure at least one asset exists

    with patch("app.services.storage.storage_service.generate_presigned_url", return_value="http://fake/thumb"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                SEARCH_URL,
                params={"q": ""},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) > 0
    assert data["next_cursor"] is None


@pytest.mark.asyncio
async def test_no_match_returns_empty(migrator_engine, user_token):
    with patch("app.services.storage.storage_service.generate_presigned_url", return_value="http://fake/thumb"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                SEARCH_URL,
                params={"q": "xyzzy_no_such_term_42"},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 200
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_rls_isolation(migrator_engine, user_token, other_user_token):
    """Assets belonging to another user are never returned."""
    other_owner_id = _owner_id(migrator_engine, other_user_token)
    unique_word = f"xsecret{uuid.uuid4().hex[:8]}"
    _insert_asset(migrator_engine, other_owner_id, description=f"Photo with {unique_word}")

    with patch("app.services.storage.storage_service.generate_presigned_url", return_value="http://fake/thumb"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                SEARCH_URL,
                params={"q": unique_word},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 200
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_unauthenticated_returns_401():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(SEARCH_URL, params={"q": "sunset"})

    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_search_by_camera_make(migrator_engine, user_token):
    owner_id = _owner_id(migrator_engine, user_token)
    asset_id = _insert_asset(migrator_engine, owner_id, make="Sony")

    with patch("app.services.storage.storage_service.generate_presigned_url", return_value="http://fake/thumb"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                SEARCH_URL,
                params={"q": "Sony"},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 200
    ids = [i["id"] for i in resp.json()["items"]]
    assert asset_id in ids


@pytest.mark.asyncio
async def test_search_by_camera_model(migrator_engine, user_token):
    owner_id = _owner_id(migrator_engine, user_token)
    asset_id = _insert_asset(migrator_engine, owner_id, make="Canon", model="EOS R5")

    with patch("app.services.storage.storage_service.generate_presigned_url", return_value="http://fake/thumb"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                SEARCH_URL,
                params={"q": "EOS R5"},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 200
    ids = [i["id"] for i in resp.json()["items"]]
    assert asset_id in ids
