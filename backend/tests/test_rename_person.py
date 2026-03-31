"""Integration tests for PATCH /people/{id} (rename person).

Covers:
  1. Happy path — renames the person, returns updated id + name
  2. 404 when tag does not exist
  3. 409 when target name is already taken by another tag
  4. 422 when name is blank
  5. RLS isolation — cannot rename another user's person
  6. Unauthenticated request → 401

Requires the test PostgreSQL container from docker-compose.test.yml.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_rename_person.py -v
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
    email = f"admin-rename-{uuid.uuid4().hex[:8]}@test.com"
    password = "AdminP@ss1!"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            REGISTER_URL,
            json={"email": email, "display_name": "Admin", "password": password},
        )
        assert resp.status_code == 201, resp.text
    with migrator_engine.begin() as conn:
        conn.execute(text("UPDATE users SET role = 'admin' WHERE email = :e"), {"e": email})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(LOGIN_URL, json={"email": email, "password": password})
        assert resp.status_code == 200
        return resp.json()["access_token"]


@pytest.fixture(scope="module")
async def user_token(migrator_engine, admin_token: str) -> str:
    email = f"user-rename-{uuid.uuid4().hex[:8]}@test.com"
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
    email = f"other-rename-{uuid.uuid4().hex[:8]}@test.com"
    password = "OtherP@ss1!"
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
                "display_name": "Other",
                "password": password,
                "invitation_token": invitation_token,
            },
        )
        assert resp.status_code == 201, resp.text
        return resp.json()["access_token"]


def _insert_tag(engine, owner_email: str, name: str) -> str:
    """Insert a tag directly and return its UUID string."""
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT id FROM users WHERE email = :e"), {"e": owner_email}
        ).fetchone()
        owner_id = str(row[0])
        tag_id = str(uuid.uuid4())
        conn.execute(
            text("INSERT INTO tags (id, owner_id, name) VALUES (:id, :owner_id, :name)"),
            {"id": tag_id, "owner_id": owner_id, "name": name},
        )
    return tag_id


@pytest.fixture(scope="module")
def user_ids(migrator_engine, user_token: str, other_user_token: str) -> dict:
    """Resolve user IDs for the two test users after both are created.

    Uses the same ordering strategy as test_asset_detail.py: the two most
    recently created 'user'-role users are other_user (rows[0]) and user (rows[1]).
    """
    with migrator_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, email FROM users WHERE role = 'user' ORDER BY created_at DESC LIMIT 2")
        ).fetchall()
    other_user_id = str(rows[0][0])
    other_user_email = rows[0][1]
    user_id = str(rows[1][0])
    user_email = rows[1][1]
    return {
        "user_id": user_id,
        "user_email": user_email,
        "other_user_id": other_user_id,
        "other_user_email": other_user_email,
    }


@pytest.fixture
async def person_tag(migrator_engine, user_ids: dict):
    """A fresh tag owned by the test user, deleted after each test."""
    tag_id = _insert_tag(migrator_engine, user_ids["user_email"], f"Alice-{uuid.uuid4().hex[:6]}")
    yield tag_id
    with migrator_engine.begin() as conn:
        conn.execute(text("DELETE FROM tags WHERE id = :id"), {"id": tag_id})


@pytest.mark.asyncio
async def test_rename_person_happy_path(user_token: str, person_tag: str, migrator_engine):
    with patch("app.services.storage.storage_service.generate_presigned_url", return_value="http://example.com/thumb.webp"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                f"/people/{person_tag}",
                json={"name": "Alice Smith"},
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == person_tag
    assert data["name"] == "Alice Smith"
    # Verify DB updated
    with migrator_engine.connect() as conn:
        row = conn.execute(
            text("SELECT name FROM tags WHERE id = :id"), {"id": person_tag}
        ).fetchone()
    assert row[0] == "Alice Smith"


@pytest.mark.asyncio
async def test_rename_person_not_found(user_token: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch(
            f"/people/{uuid.uuid4()}",
            json={"name": "Ghost"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rename_person_name_conflict(user_token: str, migrator_engine, user_ids: dict):
    taken_name = f"Taken-{uuid.uuid4().hex[:6]}"
    source_id = _insert_tag(migrator_engine, user_ids["user_email"], f"Source-{uuid.uuid4().hex[:6]}")
    taken_id = _insert_tag(migrator_engine, user_ids["user_email"], taken_name)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.patch(
                f"/people/{source_id}",
                json={"name": taken_name},
                headers={"Authorization": f"Bearer {user_token}"},
            )
        assert resp.status_code == 409
    finally:
        with migrator_engine.begin() as conn:
            conn.execute(text("DELETE FROM tags WHERE id IN (:a, :b)"), {"a": source_id, "b": taken_id})


@pytest.mark.asyncio
async def test_rename_person_blank_name(user_token: str, person_tag: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch(
            f"/people/{person_tag}",
            json={"name": "   "},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_rename_person_rls_isolation(other_user_token: str, person_tag: str, user_ids: dict):
    """Another user cannot rename a tag they don't own — RLS makes it invisible → 404."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch(
            f"/people/{person_tag}",
            json={"name": "Hacker"},
            headers={"Authorization": f"Bearer {other_user_token}"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rename_person_unauthenticated(person_tag: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.patch(f"/people/{person_tag}", json={"name": "Anyone"})
    assert resp.status_code == 401
