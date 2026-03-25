"""Tests for Takeout folder-to-album import logic (issue #75).

Covers:
  Unit tests (no DB):
    1. _zip_folder_path — extracts parent folder from zip entry path
    2. _zip_folder_path — returns None for root-level entries

  Integration tests (real DB, async session):
    3. _get_or_create_album creates a new album and returns its id
    4. _get_or_create_album is idempotent — same id on second call
    5. _ensure_album_path creates a single-segment album
    6. _ensure_album_path creates a nested two-segment hierarchy
    7. _ensure_album_path returns None for empty/dot path
    8. Re-importing the same path yields the same album ids (idempotency)
    9. Two different paths that share a root segment reuse the same root album
    10. _link_asset_to_album ON CONFLICT DO NOTHING — no error on duplicate

Requires the test PostgreSQL container from docker-compose.test.yml.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_takeout_albums.py -v
"""

from __future__ import annotations

import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.worker.takeout_tasks import (
    _ensure_album_path,
    _get_or_create_album,
    _link_asset_to_album,
    _zip_folder_path,
)

MIGRATOR_URL = os.environ.get(
    "TEST_DATABASE_MIGRATOR_URL",
    "postgresql+psycopg://migrator:testpassword@localhost:5433/photo_test",
)
APP_USER_ASYNC_URL = os.environ.get(
    "TEST_DATABASE_APP_ASYNC_URL",
    "postgresql+psycopg://app_user:testpassword@localhost:5433/photo_test",
)


# ---------------------------------------------------------------------------
# Schema and seed fixtures
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


@pytest.fixture(scope="module")
def owner_id(migrator_engine) -> uuid.UUID:
    """Create a test user and return their id. Cleaned up with the migration downgrade."""
    uid = uuid.uuid4()
    with migrator_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, password_hash)"
                " VALUES (:id, :email, :name, 'x')"
            ),
            {"id": uid, "email": f"album-test-{uid}@example.com", "name": "Album Tester"},
        )
    return uid


@pytest.fixture(scope="module")
def session_factory():
    """Async session factory connected as app_user (RLS enforced)."""
    engine = create_async_engine(APP_USER_ASYNC_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    # Engine disposal happens when the event loop tears down — no explicit call needed here


# ---------------------------------------------------------------------------
# Unit tests — pure logic, no DB
# ---------------------------------------------------------------------------


class TestZipFolderPath:
    def test_nested_two_levels(self):
        assert _zip_folder_path("2001/january/photo.jpg") == "2001/january"

    def test_nested_one_level(self):
        assert _zip_folder_path("2001/photo.jpg") == "2001"

    def test_root_level_returns_none(self):
        assert _zip_folder_path("photo.jpg") is None

    def test_deep_nesting(self):
        assert _zip_folder_path("a/b/c/d/img.heic") == "a/b/c/d"

    def test_root_dot_returns_none(self):
        # PurePosixPath("photo.jpg").parent == PurePosixPath(".")
        assert _zip_folder_path("photo.jpg") is None


# ---------------------------------------------------------------------------
# Integration tests — DB required
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetOrCreateAlbum:
    async def test_creates_new_root_album(self, session_factory, owner_id):
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(f"SET LOCAL app.current_user_id = '{owner_id}'")
                )
                album_id = await _get_or_create_album(session, owner_id, None, "2001")
        assert isinstance(album_id, uuid.UUID)

    async def test_idempotent_root_album(self, session_factory, owner_id):
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(f"SET LOCAL app.current_user_id = '{owner_id}'")
                )
                id1 = await _get_or_create_album(session, owner_id, None, "idempotent-root")
                id2 = await _get_or_create_album(session, owner_id, None, "idempotent-root")
        assert id1 == id2

    async def test_creates_child_album(self, session_factory, owner_id):
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(f"SET LOCAL app.current_user_id = '{owner_id}'")
                )
                root_id = await _get_or_create_album(session, owner_id, None, "parent-album")
                child_id = await _get_or_create_album(session, owner_id, root_id, "child-album")
        assert child_id != root_id

    async def test_same_title_different_parents_are_different_albums(self, session_factory, owner_id):
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(f"SET LOCAL app.current_user_id = '{owner_id}'")
                )
                root_a = await _get_or_create_album(session, owner_id, None, "root-a")
                root_b = await _get_or_create_album(session, owner_id, None, "root-b")
                child_a = await _get_or_create_album(session, owner_id, root_a, "shared-name")
                child_b = await _get_or_create_album(session, owner_id, root_b, "shared-name")
        assert child_a != child_b


@pytest.mark.asyncio
class TestEnsureAlbumPath:
    async def test_single_segment(self, session_factory, owner_id):
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(f"SET LOCAL app.current_user_id = '{owner_id}'")
                )
                album_id = await _ensure_album_path(session, owner_id, "single-seg")
        assert album_id is not None

    async def test_two_segments_creates_hierarchy(self, session_factory, owner_id, migrator_engine):
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(f"SET LOCAL app.current_user_id = '{owner_id}'")
                )
                leaf_id = await _ensure_album_path(session, owner_id, "year2001/january")

        with migrator_engine.connect() as conn:
            row = conn.execute(
                text("SELECT parent_id, title FROM albums WHERE id = :id"),
                {"id": leaf_id},
            ).fetchone()
        assert row is not None
        assert row.title == "january"
        assert row.parent_id is not None

        with migrator_engine.connect() as conn:
            parent_row = conn.execute(
                text("SELECT parent_id, title FROM albums WHERE id = :id"),
                {"id": row.parent_id},
            ).fetchone()
        assert parent_row.title == "year2001"
        assert parent_row.parent_id is None

    async def test_empty_path_returns_none(self, session_factory, owner_id):
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(f"SET LOCAL app.current_user_id = '{owner_id}'")
                )
                result = await _ensure_album_path(session, owner_id, ".")
        assert result is None

    async def test_idempotent_reimport(self, session_factory, owner_id):
        """Re-importing the same path returns the same album ids and creates no duplicates."""
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(f"SET LOCAL app.current_user_id = '{owner_id}'")
                )
                id1 = await _ensure_album_path(session, owner_id, "idem-year/idem-month")

        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(f"SET LOCAL app.current_user_id = '{owner_id}'")
                )
                id2 = await _ensure_album_path(session, owner_id, "idem-year/idem-month")

        assert id1 == id2

    async def test_shared_root_across_paths(self, session_factory, owner_id, migrator_engine):
        """Two paths sharing a root segment should reuse the same root album."""
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(f"SET LOCAL app.current_user_id = '{owner_id}'")
                )
                leaf_jan = await _ensure_album_path(session, owner_id, "shared-root/jan")
                leaf_feb = await _ensure_album_path(session, owner_id, "shared-root/feb")

        with migrator_engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT parent_id FROM albums"
                    " WHERE id IN (:jan, :feb)"
                ),
                {"jan": leaf_jan, "feb": leaf_feb},
            ).fetchall()
        parent_ids = {r.parent_id for r in rows}
        assert len(parent_ids) == 1


@pytest.mark.asyncio
class TestLinkAssetToAlbum:
    async def test_link_and_duplicate_no_error(self, session_factory, owner_id, migrator_engine):
        """Linking the same asset twice should not raise an error."""
        asset_id = uuid.uuid4()
        with migrator_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO media_assets"
                    " (id, owner_id, file_size_bytes, original_filename,"
                    "  mime_type, storage_key, checksum)"
                    " VALUES (:id, :owner_id, 1, 'test.jpg',"
                    "  'image/jpeg', :key, :checksum)"
                ),
                {
                    "id": asset_id,
                    "owner_id": owner_id,
                    "key": f"{owner_id}/{asset_id}/original.jpg",
                    "checksum": "aa" * 32,
                },
            )

        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(f"SET LOCAL app.current_user_id = '{owner_id}'")
                )
                album_id = await _ensure_album_path(session, owner_id, "link-test")
                await _link_asset_to_album(session, album_id, asset_id)
                # Second call — ON CONFLICT DO NOTHING
                await _link_asset_to_album(session, album_id, asset_id)

        with migrator_engine.connect() as conn:
            count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM album_assets"
                    " WHERE album_id = :album_id AND asset_id = :asset_id"
                ),
                {"album_id": album_id, "asset_id": asset_id},
            ).scalar()
        assert count == 1
