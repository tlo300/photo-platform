"""Integration tests for the direct upload API (issue #91).

Covers:
  1. POST /upload — single valid image → 202 + job_id
  2. POST /upload — multiple files → 202
  3. POST /upload — unsupported file type (all invalid) → 422 with per-file errors
  4. POST /upload — mixed valid + invalid files → 202; invalid pre-recorded in job.errors
  5. POST /upload — unauthenticated → 401
  6. POST /upload — no files → 422
  7. POST /upload — with album_id → 202
  8. GET /import/jobs/{job_id} — job created by upload is visible to owner
  9. GET /import/jobs/{job_id} — another user cannot see upload job (RLS)

Celery tasks and S3 are mocked; no Redis or MinIO required.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_upload_api.py -v
"""

import io
import os
import struct
import uuid
import zlib
from unittest.mock import MagicMock, patch

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

UPLOAD_URL = "/upload"
REGISTER_URL = "/auth/register"
LOGIN_URL = "/auth/login"


# ---------------------------------------------------------------------------
# Alembic / schema fixtures
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
    import hashlib
    import secrets
    from datetime import datetime, timedelta, timezone

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
    admin_email = f"admin-upload-{uuid.uuid4().hex[:8]}@test.com"
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


@pytest.fixture
async def user_token(migrator_engine, admin_token: str) -> str:
    email = f"user-upload-{uuid.uuid4().hex[:8]}@test.com"
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


@pytest.fixture
async def second_user_token(migrator_engine, admin_token: str) -> str:
    email = f"user2-upload-{uuid.uuid4().hex[:8]}@test.com"
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
# Minimal media byte helpers
# ---------------------------------------------------------------------------


def _minimal_jpeg() -> bytes:
    """Return the smallest valid JPEG magic header (enough for filetype detection)."""
    return (
        b"\xff\xd8\xff\xe0"  # SOI + APP0 marker
        b"\x00\x10JFIF\x00"  # APP0 length + identifier
        b"\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xd9"  # EOI
    )


def _minimal_png() -> bytes:
    """Return a 1×1 white PNG (valid magic bytes for filetype)."""
    # PNG signature
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(name: bytes, data: bytes) -> bytes:
        length = struct.pack(">I", len(data))
        crc = struct.pack(">I", zlib.crc32(name + data) & 0xFFFFFFFF)
        return length + name + data + crc

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw_row = b"\x00\xff\xff\xff"  # filter byte + RGB white
    compressed = zlib.compress(raw_row)
    idat = chunk(b"IDAT", compressed)
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


# ---------------------------------------------------------------------------
# Tests — POST /upload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_single_file_returns_job_id(user_token: str):
    """Valid single JPEG upload → 202 with job_id."""
    with (
        patch("app.api.upload.storage_service._client") as mock_s3,
        patch("app.api.upload.process_direct_upload") as mock_task,
    ):
        mock_s3.upload_fileobj.return_value = None
        mock_task.delay.return_value = MagicMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                UPLOAD_URL,
                files=[("files", ("photo.jpg", _minimal_jpeg(), "image/jpeg"))],
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert "job_id" in body
    uuid.UUID(body["job_id"])  # must be a valid UUID


@pytest.mark.asyncio
async def test_upload_multiple_files_returns_job_id(user_token: str):
    """Multiple files in one request → 202 with single job_id."""
    with (
        patch("app.api.upload.storage_service._client") as mock_s3,
        patch("app.api.upload.process_direct_upload") as mock_task,
    ):
        mock_s3.upload_fileobj.return_value = None
        mock_task.delay.return_value = MagicMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                UPLOAD_URL,
                files=[
                    ("files", ("a.jpg", _minimal_jpeg(), "image/jpeg")),
                    ("files", ("b.png", _minimal_png(), "image/png")),
                ],
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 202, resp.text
    assert "job_id" in resp.json()


@pytest.mark.asyncio
async def test_upload_unsupported_type_returns_422(user_token: str):
    """A plain text file → 422 with per-file error list."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            UPLOAD_URL,
            files=[("files", ("doc.txt", b"Hello world", "text/plain"))],
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 422
    body = resp.json()
    assert "errors" in body
    assert len(body["errors"]) == 1
    assert body["errors"][0]["filename"] == "doc.txt"


@pytest.mark.asyncio
async def test_upload_mixed_files_partial_success(user_token: str):
    """Valid + invalid files in one batch → 202; invalid ones recorded in job.errors."""
    with (
        patch("app.api.upload.storage_service._client") as mock_s3,
        patch("app.api.upload.process_direct_upload") as mock_task,
    ):
        mock_s3.upload_fileobj.return_value = None
        mock_task.delay.return_value = MagicMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                UPLOAD_URL,
                files=[
                    ("files", ("good.jpg", _minimal_jpeg(), "image/jpeg")),
                    ("files", ("bad.txt", b"not media", "text/plain")),
                ],
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]

    # The job should have the invalid file pre-recorded as an error
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        job_resp = await client.get(
            f"/import/jobs/{job_id}",
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert job_resp.status_code == 200
    job = job_resp.json()
    assert len(job["errors"]) == 1
    assert job["errors"][0]["filename"] == "bad.txt"


@pytest.mark.asyncio
async def test_upload_no_files_returns_400(user_token: str):
    """Empty file list → 400."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Send a form request with no files field
        resp = await client.post(
            UPLOAD_URL,
            data={},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 422  # FastAPI validation: files is required


@pytest.mark.asyncio
async def test_upload_unauthenticated():
    """No token → 401."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            UPLOAD_URL,
            files=[("files", ("photo.jpg", _minimal_jpeg(), "image/jpeg"))],
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_upload_with_album_id_returns_job_id(user_token: str, migrator_engine):
    """Providing a valid album_id query param → 202."""
    # Create an album via the albums API first
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        album_resp = await client.post(
            "/albums",
            json={"title": "Test Album"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert album_resp.status_code == 201, album_resp.text
    album_id = album_resp.json()["id"]

    with (
        patch("app.api.upload.storage_service._client") as mock_s3,
        patch("app.api.upload.process_direct_upload") as mock_task,
    ):
        mock_s3.upload_fileobj.return_value = None
        mock_task.delay.return_value = MagicMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"{UPLOAD_URL}?album_id={album_id}",
                files=[("files", ("photo.jpg", _minimal_jpeg(), "image/jpeg"))],
                headers={"Authorization": f"Bearer {user_token}"},
            )

    assert resp.status_code == 202, resp.text


# ---------------------------------------------------------------------------
# Tests — GET /import/jobs/{job_id} with upload jobs
# ---------------------------------------------------------------------------


@pytest.fixture
async def upload_job_id(user_token: str) -> str:
    """Create an upload job and return its job_id."""
    with (
        patch("app.api.upload.storage_service._client") as mock_s3,
        patch("app.api.upload.process_direct_upload") as mock_task,
    ):
        mock_s3.upload_fileobj.return_value = None
        mock_task.delay.return_value = MagicMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                UPLOAD_URL,
                files=[("files", ("photo.jpg", _minimal_jpeg(), "image/jpeg"))],
                headers={"Authorization": f"Bearer {user_token}"},
            )
    assert resp.status_code == 202
    return resp.json()["job_id"]


@pytest.mark.asyncio
async def test_get_upload_job_owner_can_poll(user_token: str, upload_job_id: str):
    """Owner can poll a job created by POST /upload."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/import/jobs/{upload_job_id}",
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == upload_job_id
    assert body["status"] in ("pending", "processing", "done", "failed")


@pytest.mark.asyncio
async def test_get_upload_job_other_user_cannot_see(
    second_user_token: str, upload_job_id: str
):
    """Another user gets 404 for an upload job they don't own (RLS)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/import/jobs/{upload_job_id}",
            headers={"Authorization": f"Bearer {second_user_token}"},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests — _ingest_one duplicate album-linking
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ingest_session_factory():
    """Async session factory connected as app_user (RLS enforced)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    APP_USER_ASYNC_URL = os.environ.get(
        "TEST_DATABASE_APP_ASYNC_URL",
        "postgresql+psycopg://app_user:testpassword@localhost:5433/photo_test",
    )
    engine = create_async_engine(APP_USER_ASYNC_URL)
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(scope="module")
def ingest_owner_id(migrator_engine) -> uuid.UUID:
    uid = uuid.uuid4()
    with migrator_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, password_hash)"
                " VALUES (:id, :email, :name, 'x')"
            ),
            {"id": uid, "email": f"ingest-test-{uid}@example.com", "name": "Ingest Tester"},
        )
    return uid


def _make_job(owner_id: uuid.UUID):
    from app.models.import_job import ImportJob, ImportJobStatus

    j = ImportJob()
    j.id = uuid.uuid4()
    j.owner_id = owner_id
    j.status = ImportJobStatus.processing
    j.total = 1
    j.processed = 0
    j.duplicates = 0
    j.no_sidecar = 0
    j.errors = []
    j.upload_keys = []
    return j


@pytest.mark.asyncio
async def test_duplicate_photo_linked_to_new_album(
    ingest_session_factory, ingest_owner_id, migrator_engine
):
    """A photo that already exists should be linked to the new album on re-upload.

    Regression test: previously the dedup early-return skipped album linking entirely,
    so the existing photo was NOT added to the new album.
    """
    import hashlib
    from sqlalchemy import select as sa_select

    from app.models.album import AlbumAsset
    from app.models.media import MediaAsset
    from app.services.exif import ExifResult
    from app.worker.upload_tasks import _ingest_one

    # Unique bytes so the checksum doesn't collide with other test runs
    jpeg = _minimal_jpeg() + uuid.uuid4().bytes

    owner_id = ingest_owner_id
    session_factory = ingest_session_factory

    # Create two albums directly in the DB (bypass RLS via migrator)
    album_a_id = uuid.uuid4()
    album_b_id = uuid.uuid4()
    with migrator_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO albums (id, owner_id, title) VALUES"
                " (:a, :owner, 'Dup-Test Album A'), (:b, :owner, 'Dup-Test Album B')"
            ),
            {"a": album_a_id, "b": album_b_id, "owner": owner_id},
        )

    fake_exif = ExifResult()

    # First ingest — creates the asset and links it to album A
    job1 = _make_job(owner_id)
    with (
        patch("app.worker.upload_tasks.storage_service") as mock_storage,
        patch("app.worker.upload_tasks.extract_exif", return_value=fake_exif),
        patch("app.worker.upload_tasks.apply_exif"),
        patch("app.worker.thumbnail_tasks.generate_thumbnails") as mock_thumbs,
    ):
        mock_storage.upload.return_value = f"{owner_id}/{uuid.uuid4()}/original.jpg"
        mock_thumbs.delay.return_value = None

        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(f"SET LOCAL app.current_user_id = '{owner_id}'")
                )
                await _ingest_one(
                    session, job1, owner_id, jpeg, "photo.jpg", "",
                    target_album_id=album_a_id,
                )

    assert job1.duplicates == 0

    # Second ingest — same bytes, different target album
    job2 = _make_job(owner_id)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(f"SET LOCAL app.current_user_id = '{owner_id}'")
            )
            await _ingest_one(
                session, job2, owner_id, jpeg, "photo.jpg", "",
                target_album_id=album_b_id,
            )

    assert job2.duplicates == 1

    # Verify the asset is now in BOTH albums
    checksum = hashlib.sha256(jpeg).hexdigest()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(f"SET LOCAL app.current_user_id = '{owner_id}'")
            )
            asset_id = await session.scalar(
                sa_select(MediaAsset.id).where(
                    MediaAsset.owner_id == owner_id,
                    MediaAsset.checksum == checksum,
                )
            )
            assert asset_id is not None, "asset should exist after first ingest"

            memberships = set(
                await session.scalars(
                    sa_select(AlbumAsset.album_id).where(AlbumAsset.asset_id == asset_id)
                )
            )

    assert album_a_id in memberships, "photo should still be in original album A"
    assert album_b_id in memberships, "photo should have been added to new album B"
