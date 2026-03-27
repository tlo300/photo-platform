"""Tests for POST /upload/single (issue #104).

Covers:
  1. Happy path: photo only → 201, is_live_photo=False
  2. Happy path: photo + live video → 201, is_live_photo=True
  3. Error: unsupported MIME on photo → 422
  4. Error: unsupported MIME on live video → 422

StorageService and Celery tasks are mocked; no real MinIO or Redis required.
The test database (docker-compose.test.yml) is used for the DB layer.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_upload_single.py -v
"""

from __future__ import annotations

import hashlib
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

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
SINGLE_UPLOAD_URL = "/upload/single"


# ---------------------------------------------------------------------------
# Minimal valid magic-byte headers for MIME detection
# ---------------------------------------------------------------------------

# JPEG: starts with FF D8 FF
_JPEG_HEADER = (
    b"\xff\xd8\xff\xe0"  # SOI + APP0 marker
    + b"\x00\x10"        # APP0 length
    + b"JFIF\x00"        # identifier
    + b"\x01\x01"        # version
    + b"\x00\x00\x01\x00\x01\x00\x00"  # aspect + thumbnail size
    + b"\x00" * 480      # pad to 512 bytes
)

# MP4: ftyp box at offset 4 with 'mp4 ' or 'isom' brand
_MP4_HEADER = (
    b"\x00\x00\x00\x18"  # box size = 24
    b"ftyp"               # box type
    b"isom"               # major brand
    b"\x00\x00\x02\x00"  # minor version
    b"isom"               # compatible brand
    + b"\x00" * 488       # pad to 512 bytes
)

# MOV: ftyp box with 'qt  ' brand (QuickTime)
_MOV_HEADER = (
    b"\x00\x00\x00\x14"  # box size = 20
    b"ftyp"               # box type
    b"qt  "               # major brand (QuickTime)
    b"\x00\x00\x02\x00"  # minor version
    + b"\x00" * 492       # pad to 512 bytes
)

# Plain text (unsupported)
_TEXT_BYTES = b"Hello, world! This is not a valid image file." + b" " * 467


# ---------------------------------------------------------------------------
# Alembic schema fixtures
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
    admin_email = f"admin-single-{uuid.uuid4().hex[:8]}@test.com"
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
    email = f"user-single-{uuid.uuid4().hex[:8]}@test.com"
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


# ---------------------------------------------------------------------------
# Helpers for patching storage + celery
# ---------------------------------------------------------------------------


def _make_storage_mock():
    """Return a mock storage service that returns predictable keys."""
    mock = MagicMock()
    mock.upload.return_value = "user-id/asset-id/original.jpg"
    mock.upload_live_video.return_value = "user-id/asset-id/live.mp4"
    mock.generate_presigned_url.return_value = "https://example.com/photo.jpg"
    mock.presigned_live_url.return_value = "https://example.com/live.mp4"
    return mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_single_photo_only(user_token: str):
    """Photo-only upload returns 201 with is_live_photo=False."""
    with (
        patch("app.services.media.storage_service", _make_storage_mock()),
        patch("app.api.upload.generate_thumbnails") as mock_thumb,
    ):
        mock_thumb.delay = MagicMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                SINGLE_UPLOAD_URL,
                headers={"Authorization": f"Bearer {user_token}"},
                files={"photo": ("photo.jpg", _JPEG_HEADER, "image/jpeg")},
            )

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert "asset_id" in data
    assert data["is_live_photo"] is False
    # Thumbnail task should have been enqueued
    mock_thumb.delay.assert_called_once()


@pytest.mark.asyncio
async def test_upload_single_photo_with_live_video(user_token: str):
    """Photo + live video upload returns 201 with is_live_photo=True."""
    storage_mock = _make_storage_mock()
    with (
        patch("app.services.media.storage_service", storage_mock),
        patch("app.api.upload.generate_thumbnails") as mock_thumb,
    ):
        mock_thumb.delay = MagicMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                SINGLE_UPLOAD_URL,
                headers={"Authorization": f"Bearer {user_token}"},
                files={
                    "photo": ("photo.jpg", _JPEG_HEADER, "image/jpeg"),
                    "live_video": ("live.mp4", _MP4_HEADER, "video/mp4"),
                },
            )

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert "asset_id" in data
    assert data["is_live_photo"] is True
    mock_thumb.delay.assert_called_once()


@pytest.mark.asyncio
async def test_upload_single_unsupported_photo_mime(user_token: str):
    """Unsupported photo MIME type (text) returns 422."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            SINGLE_UPLOAD_URL,
            headers={"Authorization": f"Bearer {user_token}"},
            files={"photo": ("doc.txt", _TEXT_BYTES, "text/plain")},
        )

    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_upload_single_unsupported_live_video_mime(user_token: str):
    """Valid photo + unsupported live video MIME returns 422."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            SINGLE_UPLOAD_URL,
            headers={"Authorization": f"Bearer {user_token}"},
            files={
                "photo": ("photo.jpg", _JPEG_HEADER, "image/jpeg"),
                "live_video": ("clip.txt", _TEXT_BYTES, "text/plain"),
            },
        )

    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_upload_single_unauthenticated():
    """No token → 401."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            SINGLE_UPLOAD_URL,
            files={"photo": ("photo.jpg", _JPEG_HEADER, "image/jpeg")},
        )
    assert resp.status_code == 401
