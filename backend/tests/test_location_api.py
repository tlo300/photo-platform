"""Integration tests for GET /assets near/radius_km filter (issue #43).

Covers:
  1.  near filter returns assets within radius
  2.  near filter excludes assets outside radius
  3.  near filter results ordered by distance (closest first)
  4.  Assets without a location row excluded when near is used
  5.  RLS isolation — near filter does not leak other users' assets
  6.  Unauthenticated request → 401
  7.  Invalid near format → 400

Requires the test PostgreSQL container from docker-compose.test.yml.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_location_api.py -v
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
    email = f"admin-loc-{uuid.uuid4().hex[:8]}@test.com"
    password = "AdminP@ss1!"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            REGISTER_URL,
            json={"email": email, "display_name": "Admin", "password": password},
        )
        assert resp.status_code == 201, resp.text

    with migrator_engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET role = 'admin' WHERE email = :e"),
            {"e": email},
        )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(LOGIN_URL, json={"email": email, "password": password})
        assert resp.status_code == 200
        return resp.json()["access_token"]


@pytest.fixture(scope="module")
async def user_token(migrator_engine, admin_token: str) -> str:
    email = f"user-loc-{uuid.uuid4().hex[:8]}@test.com"
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
    email = f"user2-loc-{uuid.uuid4().hex[:8]}@test.com"
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

_NOW = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _insert_asset(
    engine,
    owner_id: str,
    filename: str,
    *,
    mime_type: str = "image/jpeg",
    captured_at: datetime | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> str:
    """Insert a media_asset (and optionally a location row); return the asset UUID."""
    asset_id = str(uuid.uuid4())
    storage_key = f"{owner_id}/{asset_id}/original.jpg"
    checksum = hashlib.sha256(filename.encode()).hexdigest()

    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO media_assets"
                " (id, owner_id, original_filename, mime_type, storage_key,"
                "  file_size_bytes, checksum, captured_at)"
                " VALUES (:id, :owner_id, :fn, :mime, :key, :size, :chk, :cat)"
            ),
            {
                "id": asset_id,
                "owner_id": owner_id,
                "fn": filename,
                "mime": mime_type,
                "key": storage_key,
                "size": 1024,
                "chk": checksum,
                "cat": captured_at,
            },
        )

        if lat is not None and lon is not None:
            conn.execute(
                text(
                    "INSERT INTO locations (asset_id, point)"
                    " VALUES (:a, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326))"
                ),
                {"a": asset_id, "lat": lat, "lon": lon},
            )

    return asset_id


def _get_user_ids(engine, email_fragments: list[str]) -> list[str]:
    """Look up user ids by email fragment (most recent match per fragment)."""
    ids = []
    for fragment in email_fragments:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT id FROM users WHERE email LIKE :f ORDER BY created_at DESC LIMIT 1"),
                {"f": f"%{fragment}%"},
            ).fetchone()
        ids.append(str(row[0]))
    return ids


# ---------------------------------------------------------------------------
# Module-scoped test data
# ---------------------------------------------------------------------------

# Coordinates used in tests (approximate distances from Amsterdam centre):
#   amsterdam : 52.37, 4.89  — 0 km
#   utrecht   : 52.09, 5.12  — ~40 km
#   groningen : 53.22, 6.57  — ~180 km


@pytest.fixture(scope="module")
def test_data(migrator_engine, user_token, other_user_token):
    """Insert location test assets; return a mapping of names → asset UUIDs."""
    with migrator_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, email FROM users WHERE role = 'user'"
                " ORDER BY created_at DESC LIMIT 4"
            )
        ).fetchall()

    # other_user registered most recently
    other_user_id = str(rows[0][0])
    user_id = str(rows[1][0])

    amsterdam = _insert_asset(
        migrator_engine, user_id, "amsterdam.jpg",
        captured_at=_NOW - timedelta(days=3),
        lat=52.37, lon=4.89,
    )
    utrecht = _insert_asset(
        migrator_engine, user_id, "utrecht.jpg",
        captured_at=_NOW - timedelta(days=2),
        lat=52.09, lon=5.12,
    )
    groningen = _insert_asset(
        migrator_engine, user_id, "groningen.jpg",
        captured_at=_NOW - timedelta(days=1),
        lat=53.22, lon=6.57,
    )
    no_location = _insert_asset(
        migrator_engine, user_id, "no_location.jpg",
        captured_at=_NOW,
    )
    other_user_asset = _insert_asset(
        migrator_engine, other_user_id, "other_amsterdam.jpg",
        lat=52.37, lon=4.89,  # same spot but different owner
    )

    return {
        "user_id": user_id,
        "other_user_id": other_user_id,
        "amsterdam": amsterdam,
        "utrecht": utrecht,
        "groningen": groningen,
        "no_location": no_location,
        "other_user_asset": other_user_asset,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_near_returns_assets_within_radius(user_token, test_data):
    """near filter includes assets whose location falls within radius_km."""
    with patch("app.api.assets.storage_service"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                ASSETS_URL,
                params={"near": "52.37,4.89", "radius_km": 50, "limit": 200},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 200, resp.text
    ids = {a["id"] for a in resp.json()["items"]}
    assert test_data["amsterdam"] in ids   # 0 km — well within 50 km
    assert test_data["utrecht"] in ids     # ~40 km — within 50 km
    assert test_data["groningen"] not in ids  # ~180 km — outside 50 km


@pytest.mark.asyncio
async def test_near_excludes_assets_outside_radius(user_token, test_data):
    """Tighter radius excludes the further asset."""
    with patch("app.api.assets.storage_service"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                ASSETS_URL,
                params={"near": "52.37,4.89", "radius_km": 20, "limit": 200},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 200, resp.text
    ids = {a["id"] for a in resp.json()["items"]}
    assert test_data["amsterdam"] in ids   # 0 km — within 20 km
    assert test_data["utrecht"] not in ids  # ~40 km — outside 20 km
    assert test_data["groningen"] not in ids


@pytest.mark.asyncio
async def test_near_ordered_by_distance(user_token, test_data):
    """Results are returned closest-first when near is specified."""
    with patch("app.api.assets.storage_service"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                ASSETS_URL,
                params={"near": "52.37,4.89", "radius_km": 50, "limit": 200},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    ids = [a["id"] for a in items]
    # Amsterdam (0 km) must appear before Utrecht (~40 km)
    assert test_data["amsterdam"] in ids
    assert test_data["utrecht"] in ids
    assert ids.index(test_data["amsterdam"]) < ids.index(test_data["utrecht"])


@pytest.mark.asyncio
async def test_near_excludes_assets_without_location(user_token, test_data):
    """Assets with no location row are never included in near results."""
    with patch("app.api.assets.storage_service"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                ASSETS_URL,
                params={"near": "52.37,4.89", "radius_km": 5000, "limit": 200},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 200, resp.text
    ids = {a["id"] for a in resp.json()["items"]}
    assert test_data["no_location"] not in ids


@pytest.mark.asyncio
async def test_near_next_cursor_is_null(user_token, test_data):
    """next_cursor is always null for near queries (no cursor pagination)."""
    with patch("app.api.assets.storage_service"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                ASSETS_URL,
                params={"near": "52.37,4.89", "radius_km": 50, "limit": 1},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 200, resp.text
    assert resp.json()["next_cursor"] is None


@pytest.mark.asyncio
async def test_near_rls_isolation(user_token, other_user_token, test_data):
    """near filter does not return another user's assets."""
    with patch("app.api.assets.storage_service"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                ASSETS_URL,
                params={"near": "52.37,4.89", "radius_km": 5000, "limit": 200},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 200, resp.text
    ids = {a["id"] for a in resp.json()["items"]}
    assert test_data["other_user_asset"] not in ids
    assert test_data["amsterdam"] in ids


@pytest.mark.asyncio
async def test_near_invalid_format_returns_400(user_token):
    """Malformed near string → 400."""
    with patch("app.api.assets.storage_service"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                ASSETS_URL,
                params={"near": "not-valid"},
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_near_unauthenticated_returns_401():
    """No token → 401."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(ASSETS_URL, params={"near": "52.37,4.89", "radius_km": 50})
    assert resp.status_code == 401
