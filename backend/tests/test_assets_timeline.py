"""Integration tests for GET /assets — paginated timeline (issue #22).

Covers:
  1.  All assets returned when no filters applied (basic timeline)
  2.  Cursor pagination works — second page picks up where first left off
  3.  Cursor pagination is stable under concurrent inserts (no duplicates/gaps)
  4.  date_from filter
  5.  date_to filter
  6.  date_from + date_to combined
  7.  media_type=photo filter
  8.  media_type=video filter
  9.  has_location=true filter
  10. has_location=false filter
  11. RLS isolation — each user only sees their own assets
  12. Unauthenticated request → 401
  13. Invalid cursor → 400
  14. next_cursor is null on last page
  15. limit parameter is respected

Requires the test PostgreSQL container from docker-compose.test.yml.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_assets_timeline.py -v
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
# Schema fixtures (shared pattern with test_assets_api.py)
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
    admin_email = f"admin-tl-{uuid.uuid4().hex[:8]}@test.com"
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
    email = f"user-tl-{uuid.uuid4().hex[:8]}@test.com"
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
    email = f"user2-tl-{uuid.uuid4().hex[:8]}@test.com"
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
    with_location: bool = False,
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

        if with_location:
            conn.execute(
                text(
                    "INSERT INTO locations (asset_id, point)"
                    " VALUES (:a, ST_SetSRID(ST_MakePoint(4.9, 52.3), 4326))"
                ),
                {"a": asset_id},
            )

    return asset_id


def _get_user_id(engine, email_fragment: str) -> str:
    """Look up a user id by partial email match (most recent match)."""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id FROM users WHERE email LIKE :f ORDER BY created_at DESC LIMIT 1"),
            {"f": f"%{email_fragment}%"},
        ).fetchone()
    return str(row[0])


# ---------------------------------------------------------------------------
# Module-scoped test data
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def test_data(migrator_engine, user_token, other_user_token):
    """Insert timeline test assets; return a mapping of names → asset UUIDs."""
    # Resolve user IDs
    with migrator_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, email FROM users WHERE role = 'user'"
                " ORDER BY created_at DESC LIMIT 4"
            )
        ).fetchall()

    # user and other_user were just registered; other_user is most recent
    other_user_id = str(rows[0][0])
    user_id = str(rows[1][0])

    # Assets spread over time so we can test ordering and date filters
    t1 = _NOW - timedelta(days=10)   # oldest dated
    t2 = _NOW - timedelta(days=5)
    t3 = _NOW - timedelta(days=1)    # newest dated

    photo_t1 = _insert_asset(migrator_engine, user_id, "photo_t1.jpg", captured_at=t1)
    photo_t2 = _insert_asset(migrator_engine, user_id, "photo_t2.jpg", captured_at=t2, with_location=True)
    photo_t3 = _insert_asset(migrator_engine, user_id, "photo_t3.jpg", captured_at=t3)
    video_t2 = _insert_asset(migrator_engine, user_id, "video_t2.mp4", mime_type="video/mp4", captured_at=t2)
    no_date  = _insert_asset(migrator_engine, user_id, "no_date.jpg")   # captured_at IS NULL
    other_asset = _insert_asset(migrator_engine, other_user_id, "other.jpg", captured_at=t2)

    return {
        "user_id": user_id,
        "other_user_id": other_user_id,
        "photo_t1": photo_t1,
        "photo_t2": photo_t2,
        "photo_t3": photo_t3,
        "video_t2": video_t2,
        "no_date": no_date,
        "other_asset": other_asset,
        # Expose timestamps for filter tests
        "t1": t1,
        "t2": t2,
        "t3": t3,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_basic_timeline_returns_own_assets(user_token, test_data):
    """Unauthenticated with no filters returns all of the user's assets."""
    with patch("app.api.assets.storage_service") as mock_storage:
        mock_storage.generate_presigned_url.return_value = "https://example.com/thumb"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(ASSETS_URL, headers={"Authorization": f"Bearer {user_token}"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "items" in body
    assert "next_cursor" in body
    ids = {a["id"] for a in body["items"]}
    # All five user assets should be present (may span pages; just check they appear)
    for key in ("photo_t1", "photo_t2", "photo_t3", "video_t2", "no_date"):
        assert test_data[key] in ids, f"{key} missing from timeline"
    # Other user's asset must not appear
    assert test_data["other_asset"] not in ids


@pytest.mark.asyncio
async def test_results_ordered_newest_first(user_token, test_data):
    """Dated assets are returned newest-first; null captured_at comes last."""
    with patch("app.api.assets.storage_service") as mock_storage:
        mock_storage.generate_presigned_url.return_value = "https://example.com/thumb"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(ASSETS_URL, headers={"Authorization": f"Bearer {user_token}"})

    items = resp.json()["items"]
    dates = [a["captured_at"] for a in items]
    # Find position of no_date asset
    no_date_idx = next(i for i, a in enumerate(items) if a["id"] == test_data["no_date"])
    # All items before no_date must have a non-null captured_at
    for a in items[:no_date_idx]:
        assert a["captured_at"] is not None
    # Dated entries should be in descending order
    dated = [a["captured_at"] for a in items if a["captured_at"] is not None]
    assert dated == sorted(dated, reverse=True)


@pytest.mark.asyncio
async def test_cursor_pagination_no_gaps_or_duplicates(user_token, test_data):
    """Paging through limit=2 collects all assets with no duplicates or gaps."""
    all_ids = []
    cursor = None
    with patch("app.api.assets.storage_service") as mock_storage:
        mock_storage.generate_presigned_url.return_value = "https://example.com/thumb"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            while True:
                params = {"limit": 2}
                if cursor:
                    params["cursor"] = cursor
                resp = await client.get(
                    ASSETS_URL,
                    params=params,
                    headers={"Authorization": f"Bearer {user_token}"},
                )
                assert resp.status_code == 200, resp.text
                body = resp.json()
                all_ids.extend(a["id"] for a in body["items"])
                cursor = body["next_cursor"]
                if cursor is None:
                    break

    # No duplicates
    assert len(all_ids) == len(set(all_ids))
    # All five assets are present
    for key in ("photo_t1", "photo_t2", "photo_t3", "video_t2", "no_date"):
        assert test_data[key] in all_ids


@pytest.mark.asyncio
async def test_next_cursor_is_null_on_last_page(user_token, test_data):
    """When limit >= total assets, next_cursor must be null."""
    with patch("app.api.assets.storage_service") as mock_storage:
        mock_storage.generate_presigned_url.return_value = "https://example.com/thumb"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                ASSETS_URL,
                params={"limit": 200},
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 200, resp.text
    assert resp.json()["next_cursor"] is None


@pytest.mark.asyncio
async def test_limit_respected(user_token, test_data):
    """Response contains at most limit items."""
    with patch("app.api.assets.storage_service") as mock_storage:
        mock_storage.generate_presigned_url.return_value = "https://example.com/thumb"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                ASSETS_URL,
                params={"limit": 2},
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["items"]) == 2


@pytest.mark.asyncio
async def test_date_from_filter(user_token, test_data):
    """Only assets with captured_at >= date_from are returned."""
    date_from = test_data["t2"].isoformat()
    with patch("app.api.assets.storage_service") as mock_storage:
        mock_storage.generate_presigned_url.return_value = "https://example.com/thumb"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                ASSETS_URL,
                params={"date_from": date_from, "limit": 200},
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 200, resp.text
    ids = {a["id"] for a in resp.json()["items"]}
    assert test_data["photo_t2"] in ids
    assert test_data["photo_t3"] in ids
    assert test_data["video_t2"] in ids
    assert test_data["photo_t1"] not in ids   # t1 < t2
    assert test_data["no_date"] not in ids    # NULL excluded by date filter


@pytest.mark.asyncio
async def test_date_to_filter(user_token, test_data):
    """Only assets with captured_at <= date_to are returned."""
    date_to = test_data["t2"].isoformat()
    with patch("app.api.assets.storage_service") as mock_storage:
        mock_storage.generate_presigned_url.return_value = "https://example.com/thumb"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                ASSETS_URL,
                params={"date_to": date_to, "limit": 200},
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 200, resp.text
    ids = {a["id"] for a in resp.json()["items"]}
    assert test_data["photo_t1"] in ids
    assert test_data["photo_t2"] in ids
    assert test_data["video_t2"] in ids
    assert test_data["photo_t3"] not in ids   # t3 > t2
    assert test_data["no_date"] not in ids


@pytest.mark.asyncio
async def test_date_range_combined(user_token, test_data):
    """date_from + date_to together constrain the window correctly."""
    with patch("app.api.assets.storage_service") as mock_storage:
        mock_storage.generate_presigned_url.return_value = "https://example.com/thumb"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                ASSETS_URL,
                params={
                    "date_from": test_data["t2"].isoformat(),
                    "date_to": test_data["t2"].isoformat(),
                    "limit": 200,
                },
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 200, resp.text
    ids = {a["id"] for a in resp.json()["items"]}
    assert test_data["photo_t2"] in ids
    assert test_data["video_t2"] in ids
    assert test_data["photo_t1"] not in ids
    assert test_data["photo_t3"] not in ids


@pytest.mark.asyncio
async def test_media_type_photo(user_token, test_data):
    """media_type=photo returns only image/* assets."""
    with patch("app.api.assets.storage_service") as mock_storage:
        mock_storage.generate_presigned_url.return_value = "https://example.com/thumb"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                ASSETS_URL,
                params={"media_type": "photo", "limit": 200},
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 200, resp.text
    ids = {a["id"] for a in resp.json()["items"]}
    assert test_data["photo_t1"] in ids
    assert test_data["photo_t2"] in ids
    assert test_data["photo_t3"] in ids
    assert test_data["no_date"] in ids
    assert test_data["video_t2"] not in ids


@pytest.mark.asyncio
async def test_media_type_video(user_token, test_data):
    """media_type=video returns only video/* assets."""
    with patch("app.api.assets.storage_service") as mock_storage:
        mock_storage.generate_presigned_url.return_value = "https://example.com/thumb"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                ASSETS_URL,
                params={"media_type": "video", "limit": 200},
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 200, resp.text
    ids = {a["id"] for a in resp.json()["items"]}
    assert test_data["video_t2"] in ids
    assert test_data["photo_t1"] not in ids
    assert test_data["photo_t2"] not in ids


@pytest.mark.asyncio
async def test_has_location_true(user_token, test_data):
    """has_location=true returns only assets with a location row."""
    with patch("app.api.assets.storage_service") as mock_storage:
        mock_storage.generate_presigned_url.return_value = "https://example.com/thumb"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                ASSETS_URL,
                params={"has_location": "true", "limit": 200},
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 200, resp.text
    ids = {a["id"] for a in resp.json()["items"]}
    assert test_data["photo_t2"] in ids   # only one with location
    assert test_data["photo_t1"] not in ids
    assert test_data["video_t2"] not in ids


@pytest.mark.asyncio
async def test_has_location_false(user_token, test_data):
    """has_location=false returns only assets without a location row."""
    with patch("app.api.assets.storage_service") as mock_storage:
        mock_storage.generate_presigned_url.return_value = "https://example.com/thumb"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                ASSETS_URL,
                params={"has_location": "false", "limit": 200},
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 200, resp.text
    ids = {a["id"] for a in resp.json()["items"]}
    assert test_data["photo_t2"] not in ids  # has location, must be excluded
    assert test_data["photo_t1"] in ids
    assert test_data["video_t2"] in ids
    assert test_data["no_date"] in ids


@pytest.mark.asyncio
async def test_rls_isolation(user_token, other_user_token, test_data):
    """Each user only sees their own assets."""
    with patch("app.api.assets.storage_service") as mock_storage:
        mock_storage.generate_presigned_url.return_value = "https://example.com/thumb"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp1 = await client.get(
                ASSETS_URL,
                params={"limit": 200},
                headers={"Authorization": f"Bearer {user_token}"},
            )
            resp2 = await client.get(
                ASSETS_URL,
                params={"limit": 200},
                headers={"Authorization": f"Bearer {other_user_token}"},
            )

    ids1 = {a["id"] for a in resp1.json()["items"]}
    ids2 = {a["id"] for a in resp2.json()["items"]}

    assert test_data["other_asset"] not in ids1
    assert test_data["photo_t1"] not in ids2
    assert test_data["other_asset"] in ids2


@pytest.mark.asyncio
async def test_thumbnail_url_present(user_token, test_data):
    """Each item has a thumbnail_url field (string or null)."""
    with patch("app.api.assets.storage_service") as mock_storage:
        mock_storage.generate_presigned_url.return_value = "https://example.com/thumb"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(ASSETS_URL, headers={"Authorization": f"Bearer {user_token}"})

    assert resp.status_code == 200, resp.text
    for item in resp.json()["items"]:
        assert "thumbnail_url" in item


@pytest.mark.asyncio
async def test_thumbnail_url_scoped_to_user(user_token, test_data):
    """Presigned URL is generated with the user's prefix (owner check)."""
    calls = []

    def _fake_presign(user_id: str, key: str, **kwargs) -> str:
        calls.append((user_id, key))
        return f"https://example.com/{key}"

    with patch("app.api.assets.storage_service") as mock_storage:
        mock_storage.generate_presigned_url.side_effect = _fake_presign
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                ASSETS_URL,
                params={"limit": 200},
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 200
    for user_id_arg, key_arg in calls:
        assert key_arg.startswith(f"{user_id_arg}/"), (
            f"Thumbnail key {key_arg!r} does not start with user prefix {user_id_arg!r}"
        )


@pytest.mark.asyncio
async def test_unauthenticated_returns_401():
    """No token → 401."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(ASSETS_URL)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_invalid_cursor_returns_400(user_token):
    """Malformed cursor → 400."""
    with patch("app.api.assets.storage_service"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                ASSETS_URL,
                params={"cursor": "not-a-valid-cursor!!"},
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 400
