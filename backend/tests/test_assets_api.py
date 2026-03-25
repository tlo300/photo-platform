"""Integration tests for GET /assets (issue #44).

Covers:
  1. Single person tag — asset returned for that person
  2. Multiple people tags — asset appears under each name
  3. Empty people array — asset not returned (no rows written)
  4. Person filter is case-insensitive
  5. RLS isolation — another user's assets are not returned
  6. Unauthenticated request → 401

Requires the test PostgreSQL container from docker-compose.test.yml.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_assets_api.py -v
"""

import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

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
    admin_email = f"admin-assets-{uuid.uuid4().hex[:8]}@test.com"
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
    email = f"user-assets-{uuid.uuid4().hex[:8]}@test.com"
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
    email = f"user2-assets-{uuid.uuid4().hex[:8]}@test.com"
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
# Test-data fixture
# ---------------------------------------------------------------------------


def _insert_asset_with_people(
    engine,
    owner_id: str,
    filename: str,
    people: list[str],
) -> str:
    """Insert a media_asset and people tags; return the asset UUID."""
    asset_id = str(uuid.uuid4())
    storage_key = f"{owner_id}/{asset_id}/original.jpg"
    checksum = hashlib.sha256(filename.encode()).hexdigest()

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
                "fn": filename,
                "mime": "image/jpeg",
                "key": storage_key,
                "size": 1024,
                "chk": checksum,
            },
        )

        for name in people:
            # Upsert tag
            conn.execute(
                text(
                    "INSERT INTO tags (owner_id, name) VALUES (:o, :n)"
                    " ON CONFLICT (owner_id, name) DO NOTHING"
                ),
                {"o": owner_id, "n": name},
            )
            row = conn.execute(
                text("SELECT id FROM tags WHERE owner_id = :o AND name = :n"),
                {"o": owner_id, "n": name},
            ).fetchone()
            tag_id = str(row[0])

            conn.execute(
                text(
                    "INSERT INTO asset_tags (asset_id, tag_id, source)"
                    " VALUES (:a, :t, 'google_people')"
                    " ON CONFLICT DO NOTHING"
                ),
                {"a": asset_id, "t": tag_id},
            )

    return asset_id


@pytest.fixture(scope="module")
def test_data(migrator_engine, user_token, other_user_token):
    """Insert test assets and return a dict of asset IDs and user IDs."""
    with migrator_engine.connect() as conn:
        # Fetch user IDs by matching the two most recently created non-admin users
        rows = conn.execute(
            text(
                "SELECT id, email FROM users WHERE role = 'user'"
                " ORDER BY created_at DESC LIMIT 2"
            )
        ).fetchall()

    # We registered user then other_user (module-scoped, in order)
    # other_user is most recent, user is second
    other_user_id = str(rows[0][0])
    user_id = str(rows[1][0])

    # User 1: asset tagged with Alice + Bob
    asset_alice_bob = _insert_asset_with_people(
        migrator_engine, user_id, "alice_and_bob.jpg", ["Alice", "Bob"]
    )
    # User 1: asset tagged with Charlie only
    asset_charlie = _insert_asset_with_people(
        migrator_engine, user_id, "charlie.jpg", ["Charlie"]
    )
    # User 1: asset with NO people tags (empty people array scenario)
    asset_no_people = _insert_asset_with_people(
        migrator_engine, user_id, "landscape.jpg", []
    )
    # User 2: asset also tagged with Alice (for RLS isolation test)
    asset_other_alice = _insert_asset_with_people(
        migrator_engine, other_user_id, "other_alice.jpg", ["Alice"]
    )

    return {
        "user_id": user_id,
        "other_user_id": other_user_id,
        "asset_alice_bob": asset_alice_bob,
        "asset_charlie": asset_charlie,
        "asset_no_people": asset_no_people,
        "asset_other_alice": asset_other_alice,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_person_tag(user_token, test_data):
    """Asset tagged with one person is returned for that person."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            ASSETS_URL,
            params={"person": "Charlie"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 200, resp.text
    ids = [a["id"] for a in resp.json()]
    assert test_data["asset_charlie"] in ids
    assert test_data["asset_alice_bob"] not in ids


@pytest.mark.asyncio
async def test_multiple_people_tags_first_name(user_token, test_data):
    """Asset with two people appears when querying the first person."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            ASSETS_URL,
            params={"person": "Alice"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 200, resp.text
    ids = [a["id"] for a in resp.json()]
    assert test_data["asset_alice_bob"] in ids
    assert test_data["asset_charlie"] not in ids


@pytest.mark.asyncio
async def test_multiple_people_tags_second_name(user_token, test_data):
    """Asset with two people appears when querying the second person."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            ASSETS_URL,
            params={"person": "Bob"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 200, resp.text
    ids = [a["id"] for a in resp.json()]
    assert test_data["asset_alice_bob"] in ids


@pytest.mark.asyncio
async def test_empty_people_array_not_returned(user_token, test_data):
    """Asset with no people tags does not appear in any person search."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            ASSETS_URL,
            params={"person": "Alice"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 200, resp.text
    ids = [a["id"] for a in resp.json()]
    assert test_data["asset_no_people"] not in ids


@pytest.mark.asyncio
async def test_person_filter_case_insensitive(user_token, test_data):
    """Person name filter is case-insensitive."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            ASSETS_URL,
            params={"person": "alice"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 200, resp.text
    ids = [a["id"] for a in resp.json()]
    assert test_data["asset_alice_bob"] in ids


@pytest.mark.asyncio
async def test_no_match_returns_empty_list(user_token, test_data):
    """Querying a person with no tagged assets returns an empty list."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            ASSETS_URL,
            params={"person": "Nobody"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


@pytest.mark.asyncio
async def test_rls_isolation(user_token, other_user_token, test_data):
    """Each user only sees their own assets — RLS prevents cross-user access."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # User 1 queries Alice — must not see user 2's asset
        resp1 = await client.get(
            ASSETS_URL,
            params={"person": "Alice"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp1.status_code == 200, resp1.text
        ids1 = [a["id"] for a in resp1.json()]
        assert test_data["asset_other_alice"] not in ids1

        # User 2 queries Alice — must not see user 1's asset
        resp2 = await client.get(
            ASSETS_URL,
            params={"person": "Alice"},
            headers={"Authorization": f"Bearer {other_user_token}"},
        )
        assert resp2.status_code == 200, resp2.text
        ids2 = [a["id"] for a in resp2.json()]
        assert test_data["asset_alice_bob"] not in ids2
        assert test_data["asset_other_alice"] in ids2


@pytest.mark.asyncio
async def test_unauthenticated_returns_401():
    """No token → 401."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(ASSETS_URL, params={"person": "Alice"})
    assert resp.status_code == 401
