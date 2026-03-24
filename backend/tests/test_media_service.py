"""Integration tests for MediaService.

Verifies that create_asset and delete_asset keep users.storage_used_bytes in
sync with the actual set of stored assets, and that an S3 failure on upload
leaves the database untouched.

Requires both test containers from docker-compose.test.yml:
  - PostgreSQL on localhost:5433  (photo_test DB)
  - MinIO on localhost:9002

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_media_service.py -v
"""

import io
import os
import uuid
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# Storage env vars must be set before StorageService is imported (module-level singleton).
os.environ.setdefault("STORAGE_ENDPOINT", "localhost:9002")
os.environ.setdefault("STORAGE_ACCESS_KEY", "testaccesskey")
os.environ.setdefault("STORAGE_SECRET_KEY", "testsecretkey")
os.environ.setdefault("STORAGE_BUCKET", "test-photos")
os.environ.setdefault("STORAGE_USE_SSL", "false")

from app.services.media import AssetNotFoundError, MediaService  # noqa: E402
from app.services.storage import StorageError, StorageService  # noqa: E402

MIGRATOR_URL = os.environ.get(
    "TEST_DATABASE_MIGRATOR_URL",
    "postgresql+psycopg://migrator:testpassword@localhost:5433/photo_test",
)
APP_USER_URL = os.environ.get(
    "TEST_DATABASE_APP_URL",
    "postgresql+psycopg://app_user:testpassword@localhost:5433/photo_test",
)
# asyncpg URL for async SQLAlchemy sessions (same DB, different driver)
APP_USER_ASYNC_URL = APP_USER_URL.replace(
    "postgresql+psycopg://", "postgresql+psycopg://"
)


# ---------------------------------------------------------------------------
# Schema fixtures (module-scoped — run once per pytest session for this module)
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
# Service fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def storage_svc() -> StorageService:
    svc = StorageService()
    svc.ensure_bucket_exists()
    return svc


@pytest.fixture(scope="module")
def svc(storage_svc: StorageService) -> MediaService:
    return MediaService(storage_svc)


# ---------------------------------------------------------------------------
# Per-test DB helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def user_id(migrator_engine) -> str:
    """Insert a fresh user and return their UUID string.  Cleaned up after the test."""
    uid = str(uuid.uuid4())
    with migrator_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, display_name)"
                " VALUES (:id, :email, 'Tester')"
            ),
            {"id": uid, "email": f"{uid}@test.com"},
        )
    yield uid
    with migrator_engine.begin() as conn:
        conn.execute(text("DELETE FROM media_assets WHERE owner_id = :id"), {"id": uid})
        conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": uid})


@pytest.fixture
async def authed_session(user_id: str):
    """Async SQLAlchemy session with RLS activated for *user_id*."""
    engine = create_async_engine(APP_USER_ASYNC_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(
            text(f"SET LOCAL app.current_user_id = '{user_id}'")
        )
        yield session
        await session.rollback()
    await engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_create_asset_increments_storage_bytes(svc, user_id, authed_session):
    """upload() must increment users.storage_used_bytes by the asset's file size."""
    payload = b"fake image data"
    asset_id = uuid.uuid4()

    await svc.create_asset(
        authed_session,
        user_id=uuid.UUID(user_id),
        asset_id=asset_id,
        file_obj=io.BytesIO(payload),
        suffix=".jpg",
        content_type="image/jpeg",
        file_size_bytes=len(payload),
        original_filename="photo.jpg",
        mime_type="image/jpeg",
        checksum="deadbeef",
    )

    row = (
        await authed_session.execute(
            text("SELECT storage_used_bytes FROM users WHERE id = :uid"),
            {"uid": uuid.UUID(user_id)},
        )
    ).one()
    assert row[0] == len(payload)


async def test_delete_asset_decrements_storage_bytes(svc, user_id, authed_session):
    """delete_asset() must subtract the asset size from users.storage_used_bytes."""
    payload = b"another image"
    asset_id = uuid.uuid4()

    # Create first so storage_used_bytes is non-zero.
    await svc.create_asset(
        authed_session,
        user_id=uuid.UUID(user_id),
        asset_id=asset_id,
        file_obj=io.BytesIO(payload),
        suffix=".png",
        content_type="image/png",
        file_size_bytes=len(payload),
        original_filename="photo.png",
        mime_type="image/png",
        checksum="cafebabe",
    )

    await svc.delete_asset(
        authed_session,
        user_id=uuid.UUID(user_id),
        asset_id=asset_id,
    )

    row = (
        await authed_session.execute(
            text("SELECT storage_used_bytes FROM users WHERE id = :uid"),
            {"uid": uuid.UUID(user_id)},
        )
    ).one()
    assert row[0] == 0


async def test_create_asset_s3_failure_leaves_db_unchanged(svc, user_id, authed_session):
    """If the S3 upload raises, storage_used_bytes must remain unchanged."""
    asset_id = uuid.uuid4()

    with patch.object(svc._storage, "upload", side_effect=StorageError("minio down")):
        with pytest.raises(StorageError):
            await svc.create_asset(
                authed_session,
                user_id=uuid.UUID(user_id),
                asset_id=asset_id,
                file_obj=io.BytesIO(b"data"),
                suffix=".jpg",
                content_type="image/jpeg",
                file_size_bytes=100,
                original_filename="photo.jpg",
                mime_type="image/jpeg",
                checksum="000",
            )

    row = (
        await authed_session.execute(
            text("SELECT storage_used_bytes FROM users WHERE id = :uid"),
            {"uid": uuid.UUID(user_id)},
        )
    ).one()
    assert row[0] == 0


async def test_delete_asset_raises_for_unknown_asset(svc, user_id, authed_session):
    """delete_asset() must raise AssetNotFoundError for a non-existent asset_id."""
    with pytest.raises(AssetNotFoundError):
        await svc.delete_asset(
            authed_session,
            user_id=uuid.UUID(user_id),
            asset_id=uuid.uuid4(),
        )


async def test_storage_used_bytes_floored_at_zero(svc, user_id, authed_session, migrator_engine):
    """Deleting an asset never drives storage_used_bytes below zero."""
    asset_id = uuid.uuid4()
    file_size = 500

    # Seed an asset directly (storage_used_bytes stays at 0 — simulates pre-existing data).
    key = f"{user_id}/{asset_id}/original.jpg"
    with migrator_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO media_assets"
                " (id, owner_id, file_size_bytes, original_filename, mime_type, storage_key, checksum)"
                " VALUES (:id, :owner, :size, 'old.jpg', 'image/jpeg', :key, 'abc')"
            ),
            {"id": str(asset_id), "owner": user_id, "size": file_size, "key": key},
        )

    # Upload the S3 object so delete_asset can clean up without erroring.
    svc._storage.upload(user_id, str(asset_id), io.BytesIO(b"x" * file_size), ".jpg")

    await svc.delete_asset(
        authed_session,
        user_id=uuid.UUID(user_id),
        asset_id=asset_id,
    )

    row = (
        await authed_session.execute(
            text("SELECT storage_used_bytes FROM users WHERE id = :uid"),
            {"uid": uuid.UUID(user_id)},
        )
    ).one()
    assert row[0] == 0  # GREATEST(0, 0 - 500) = 0
