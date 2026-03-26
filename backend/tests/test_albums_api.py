"""Integration tests for the Albums API (issue #27).

Covers:
  1.  POST /albums — create album
  2.  GET  /albums — list albums (cover_thumbnail_url populated when thumbnail ready)
  3.  GET  /albums/{id} — album detail with ordered asset_ids
  4.  PATCH /albums/{id} — update title
  5.  PATCH /albums/{id} — update cover_asset_id
  6.  PATCH /albums/{id} — cover asset belonging to another user → 404
  7.  DELETE /albums/{id} — deletes album but not assets
  8.  POST /albums/{id}/assets — add assets (idempotent)
  9.  POST /albums/{id}/assets — asset from another user → 404
  10. DELETE /albums/{id}/assets/{asset_id} — remove asset
  11. DELETE /albums/{id}/assets/{asset_id} — asset not in album → 404
  12. PUT /albums/{id}/assets/order — reorder assets
  13. PUT /albums/{id}/assets/order — mismatched list → 400
  14. GET  /albums/{id} — RLS: cannot access another user's album → 404
  15. POST /albums — unauthenticated → 401

Requires the test PostgreSQL container from docker-compose.test.yml.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_albums_api.py -v
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
    email = f"admin-albums-{uuid.uuid4().hex[:8]}@test.com"
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
    email = f"user-albums-{uuid.uuid4().hex[:8]}@test.com"
    password = "UserP@ss1!"
    with migrator_engine.connect() as conn:
        row = conn.execute(text("SELECT id FROM users WHERE role = 'admin' ORDER BY created_at LIMIT 1")).fetchone()
        admin_id = str(row[0])
    inv = _make_invitation(migrator_engine, email, admin_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(REGISTER_URL, json={"email": email, "display_name": "User", "password": password, "invitation_token": inv})
        assert resp.status_code == 201, resp.text
        return resp.json()["access_token"]


@pytest.fixture(scope="module")
async def other_user_token(migrator_engine, admin_token: str) -> str:
    email = f"user2-albums-{uuid.uuid4().hex[:8]}@test.com"
    password = "UserP@ss2!"
    with migrator_engine.connect() as conn:
        row = conn.execute(text("SELECT id FROM users WHERE role = 'admin' ORDER BY created_at LIMIT 1")).fetchone()
        admin_id = str(row[0])
    inv = _make_invitation(migrator_engine, email, admin_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(REGISTER_URL, json={"email": email, "display_name": "User2", "password": password, "invitation_token": inv})
        assert resp.status_code == 201, resp.text
        return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Asset helpers
# ---------------------------------------------------------------------------


def _insert_asset(engine, owner_id: str, thumbnail_ready: bool = True) -> str:
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
                "id": asset_id,
                "owner_id": owner_id,
                "fn": "photo.jpg",
                "mime": "image/jpeg",
                "key": storage_key,
                "size": 1024,
                "chk": checksum,
                "tr": thumbnail_ready,
            },
        )
    return asset_id


def _get_user_id(engine, email_fragment: str) -> str:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM users WHERE email LIKE :f ORDER BY created_at DESC LIMIT 1"),
            {"f": f"%{email_fragment}%"},
        ).fetchone()
        return str(row[0])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_album(user_token):
    """POST /albums creates an album and returns 201 with album data."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/albums",
            json={"title": "Holidays 2024"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["title"] == "Holidays 2024"
    assert data["parent_id"] is None
    assert data["cover_asset_id"] is None
    assert "id" in data
    assert "created_at" in data


@pytest.mark.asyncio
async def test_list_albums_empty(user_token, migrator_engine):
    """GET /albums returns a list; new user has no albums initially."""
    # Create a fresh user so the list is predictably empty.
    email = f"listuser-{uuid.uuid4().hex[:8]}@test.com"
    password = "UserList1!"
    with migrator_engine.connect() as conn:
        row = conn.execute(text("SELECT id FROM users WHERE role = 'admin' ORDER BY created_at LIMIT 1")).fetchone()
        admin_id = str(row[0])
    inv = _make_invitation(migrator_engine, email, admin_id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(REGISTER_URL, json={"email": email, "display_name": "List", "password": password, "invitation_token": inv})
        assert resp.status_code == 201
        token = resp.json()["access_token"]
        resp2 = await client.get("/albums", headers={"Authorization": f"Bearer {token}"})
    assert resp2.status_code == 200
    assert resp2.json() == []


@pytest.mark.asyncio
async def test_list_albums_with_cover(user_token, migrator_engine):
    """GET /albums includes cover_thumbnail_url from the first asset when thumbnail_ready."""
    # Get user_id for this token
    with migrator_engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM users WHERE email LIKE '%user-albums%' ORDER BY created_at DESC LIMIT 1")).fetchall()
        user_id = str(rows[0][0])

    asset_id = _insert_asset(migrator_engine, user_id, thumbnail_ready=True)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Create album
        resp = await client.post("/albums", json={"title": "Cover Test"}, headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 201
        album_id = resp.json()["id"]
        # Add asset
        resp = await client.post(f"/albums/{album_id}/assets", json={"asset_ids": [asset_id]}, headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 204

    fake_url = "https://example.com/thumb.webp"
    with patch("app.api.albums.storage_service.generate_presigned_url", return_value=fake_url):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/albums", headers={"Authorization": f"Bearer {user_token}"})
    assert resp.status_code == 200
    albums = resp.json()
    cover_test = next((a for a in albums if a["id"] == album_id), None)
    assert cover_test is not None
    assert cover_test["cover_thumbnail_url"] == fake_url


@pytest.mark.asyncio
async def test_get_album_detail(user_token, migrator_engine):
    """GET /albums/{id} returns ordered asset_ids."""
    with migrator_engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM users WHERE email LIKE '%user-albums%' ORDER BY created_at DESC LIMIT 1")).fetchall()
        user_id = str(rows[0][0])

    asset1 = _insert_asset(migrator_engine, user_id)
    asset2 = _insert_asset(migrator_engine, user_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/albums", json={"title": "Detail Test"}, headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 201
        album_id = resp.json()["id"]
        resp = await client.post(
            f"/albums/{album_id}/assets",
            json={"asset_ids": [asset1, asset2]},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 204

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/albums/{album_id}", headers={"Authorization": f"Bearer {user_token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == album_id
    assert set(data["asset_ids"]) == {asset1, asset2}
    assert len(data["asset_ids"]) == 2


@pytest.mark.asyncio
async def test_update_album_title(user_token):
    """PATCH /albums/{id} updates the title."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/albums", json={"title": "Old Title"}, headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 201
        album_id = resp.json()["id"]
        resp = await client.patch(f"/albums/{album_id}", json={"title": "New Title"}, headers={"Authorization": f"Bearer {user_token}"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "New Title"


@pytest.mark.asyncio
async def test_update_album_cover_asset(user_token, migrator_engine):
    """PATCH /albums/{id} with cover_asset_id sets the cover."""
    with migrator_engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM users WHERE email LIKE '%user-albums%' ORDER BY created_at DESC LIMIT 1")).fetchall()
        user_id = str(rows[0][0])
    asset_id = _insert_asset(migrator_engine, user_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/albums", json={"title": "Cover Album"}, headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 201
        album_id = resp.json()["id"]
        resp = await client.patch(
            f"/albums/{album_id}",
            json={"cover_asset_id": asset_id},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 200
    assert resp.json()["cover_asset_id"] == asset_id


@pytest.mark.asyncio
async def test_update_album_cover_other_user_asset(user_token, other_user_token, migrator_engine):
    """PATCH /albums/{id} with another user's asset → 404."""
    with migrator_engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM users WHERE email LIKE '%user2-albums%' ORDER BY created_at DESC LIMIT 1")).fetchall()
        other_user_id = str(rows[0][0])
    other_asset = _insert_asset(migrator_engine, other_user_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/albums", json={"title": "Bad Cover"}, headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 201
        album_id = resp.json()["id"]
        resp = await client.patch(
            f"/albums/{album_id}",
            json={"cover_asset_id": other_asset},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_album_does_not_delete_assets(user_token, migrator_engine):
    """DELETE /albums/{id} removes the album but assets remain in media_assets."""
    with migrator_engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM users WHERE email LIKE '%user-albums%' ORDER BY created_at DESC LIMIT 1")).fetchall()
        user_id = str(rows[0][0])
    asset_id = _insert_asset(migrator_engine, user_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/albums", json={"title": "To Delete"}, headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 201
        album_id = resp.json()["id"]
        await client.post(f"/albums/{album_id}/assets", json={"asset_ids": [asset_id]}, headers={"Authorization": f"Bearer {user_token}"})
        resp = await client.delete(f"/albums/{album_id}", headers={"Authorization": f"Bearer {user_token}"})
    assert resp.status_code == 204

    # Album is gone
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/albums/{album_id}", headers={"Authorization": f"Bearer {user_token}"})
    assert resp.status_code == 404

    # Asset still exists
    with migrator_engine.connect() as conn:
        row = conn.execute(text("SELECT id FROM media_assets WHERE id = :id"), {"id": asset_id}).fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_add_assets_idempotent(user_token, migrator_engine):
    """POST /albums/{id}/assets adding the same asset twice keeps it once."""
    with migrator_engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM users WHERE email LIKE '%user-albums%' ORDER BY created_at DESC LIMIT 1")).fetchall()
        user_id = str(rows[0][0])
    asset_id = _insert_asset(migrator_engine, user_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/albums", json={"title": "Idempotent"}, headers={"Authorization": f"Bearer {user_token}"})
        album_id = resp.json()["id"]
        resp = await client.post(f"/albums/{album_id}/assets", json={"asset_ids": [asset_id]}, headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 204
        resp = await client.post(f"/albums/{album_id}/assets", json={"asset_ids": [asset_id]}, headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 204
        resp = await client.get(f"/albums/{album_id}", headers={"Authorization": f"Bearer {user_token}"})
    assert resp.json()["asset_ids"].count(asset_id) == 1


@pytest.mark.asyncio
async def test_add_other_user_asset_rejected(user_token, other_user_token, migrator_engine):
    """POST /albums/{id}/assets with another user's asset → 404."""
    with migrator_engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM users WHERE email LIKE '%user2-albums%' ORDER BY created_at DESC LIMIT 1")).fetchall()
        other_id = str(rows[0][0])
    other_asset = _insert_asset(migrator_engine, other_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/albums", json={"title": "Reject Other"}, headers={"Authorization": f"Bearer {user_token}"})
        album_id = resp.json()["id"]
        resp = await client.post(f"/albums/{album_id}/assets", json={"asset_ids": [other_asset]}, headers={"Authorization": f"Bearer {user_token}"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_remove_asset(user_token, migrator_engine):
    """DELETE /albums/{id}/assets/{asset_id} removes the asset from the album."""
    with migrator_engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM users WHERE email LIKE '%user-albums%' ORDER BY created_at DESC LIMIT 1")).fetchall()
        user_id = str(rows[0][0])
    asset_id = _insert_asset(migrator_engine, user_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/albums", json={"title": "Remove Asset"}, headers={"Authorization": f"Bearer {user_token}"})
        album_id = resp.json()["id"]
        await client.post(f"/albums/{album_id}/assets", json={"asset_ids": [asset_id]}, headers={"Authorization": f"Bearer {user_token}"})
        resp = await client.delete(f"/albums/{album_id}/assets/{asset_id}", headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 204
        resp = await client.get(f"/albums/{album_id}", headers={"Authorization": f"Bearer {user_token}"})
    assert asset_id not in resp.json()["asset_ids"]


@pytest.mark.asyncio
async def test_remove_asset_not_in_album(user_token):
    """DELETE /albums/{id}/assets/{asset_id} for asset not in album → 404."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/albums", json={"title": "Empty Album"}, headers={"Authorization": f"Bearer {user_token}"})
        album_id = resp.json()["id"]
        resp = await client.delete(
            f"/albums/{album_id}/assets/{uuid.uuid4()}",
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reorder_assets(user_token, migrator_engine):
    """PUT /albums/{id}/assets/order reorders assets."""
    with migrator_engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM users WHERE email LIKE '%user-albums%' ORDER BY created_at DESC LIMIT 1")).fetchall()
        user_id = str(rows[0][0])
    a1 = _insert_asset(migrator_engine, user_id)
    a2 = _insert_asset(migrator_engine, user_id)
    a3 = _insert_asset(migrator_engine, user_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/albums", json={"title": "Reorder"}, headers={"Authorization": f"Bearer {user_token}"})
        album_id = resp.json()["id"]
        await client.post(f"/albums/{album_id}/assets", json={"asset_ids": [a1, a2, a3]}, headers={"Authorization": f"Bearer {user_token}"})
        # Reverse order
        resp = await client.put(
            f"/albums/{album_id}/assets/order",
            json={"asset_ids": [a3, a2, a1]},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 204
        resp = await client.get(f"/albums/{album_id}", headers={"Authorization": f"Bearer {user_token}"})
    assert resp.json()["asset_ids"] == [a3, a2, a1]


@pytest.mark.asyncio
async def test_reorder_mismatched_list(user_token, migrator_engine):
    """PUT /albums/{id}/assets/order with wrong asset set → 400."""
    with migrator_engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM users WHERE email LIKE '%user-albums%' ORDER BY created_at DESC LIMIT 1")).fetchall()
        user_id = str(rows[0][0])
    a1 = _insert_asset(migrator_engine, user_id)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/albums", json={"title": "Bad Reorder"}, headers={"Authorization": f"Bearer {user_token}"})
        album_id = resp.json()["id"]
        await client.post(f"/albums/{album_id}/assets", json={"asset_ids": [a1]}, headers={"Authorization": f"Bearer {user_token}"})
        resp = await client.put(
            f"/albums/{album_id}/assets/order",
            json={"asset_ids": [str(uuid.uuid4())]},  # wrong ID
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_rls_cannot_access_other_user_album(user_token, other_user_token):
    """GET /albums/{id} for another user's album → 404."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/albums", json={"title": "Private"}, headers={"Authorization": f"Bearer {other_user_token}"})
        assert resp.status_code == 201
        other_album_id = resp.json()["id"]
        # user tries to access other's album
        resp = await client.get(f"/albums/{other_album_id}", headers={"Authorization": f"Bearer {user_token}"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_unauthenticated_returns_401():
    """No token → 401 on POST /albums."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/albums", json={"title": "No Auth"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# is_hidden tests (issue #121)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_album_is_hidden_default_false(user_token):
    """Newly created album has is_hidden=false."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/albums",
            json={"title": "Visible Album"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 201
    assert resp.json()["is_hidden"] is False


@pytest.mark.asyncio
async def test_patch_album_is_hidden(user_token):
    """PATCH /albums/{id} with is_hidden=true persists and is reflected in list and detail."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/albums",
            json={"title": "To Hide"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        album_id = resp.json()["id"]

        resp = await client.patch(
            f"/albums/{album_id}",
            json={"is_hidden": True},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["is_hidden"] is True

        # Detail endpoint also reflects the flag.
        resp = await client.get(f"/albums/{album_id}", headers={"Authorization": f"Bearer {user_token}"})
        assert resp.json()["is_hidden"] is True

        # List endpoint too.
        resp = await client.get("/albums", headers={"Authorization": f"Bearer {user_token}"})
        found = next((a for a in resp.json() if a["id"] == album_id), None)
        assert found is not None
        assert found["is_hidden"] is True


@pytest.mark.asyncio
async def test_patch_album_unhide(user_token):
    """PATCH /albums/{id} can toggle is_hidden back to false."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/albums",
            json={"title": "Hide Then Show"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        album_id = resp.json()["id"]
        await client.patch(f"/albums/{album_id}", json={"is_hidden": True}, headers={"Authorization": f"Bearer {user_token}"})
        resp = await client.patch(f"/albums/{album_id}", json={"is_hidden": False}, headers={"Authorization": f"Bearer {user_token}"})
        assert resp.status_code == 200
        assert resp.json()["is_hidden"] is False
