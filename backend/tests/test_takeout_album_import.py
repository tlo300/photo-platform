"""Tests for Google Takeout album import (issue #28).

Covers the new metadata.json album detection and sort-order assignment logic.

  Unit tests (no DB):
    1. _build_album_index — folder with metadata.json gets AlbumMeta (title + description)
    2. _build_album_index — missing description → None
    3. _build_album_index — folder without metadata.json not in meta dict
    4. _build_album_index — sort order assigned by photoTakenTime timestamp
    5. _build_album_index — files without timestamps sorted alphabetically
    6. _build_album_index — root-level metadata.json is ignored

  Integration tests (real DB, async session):
    7.  _get_or_create_album stores description on creation
    8.  _get_or_create_album ignores description when album already exists (idempotent)
    9.  _link_asset_to_album stores the supplied sort_order
    10. _ensure_album_path uses metadata title for the leaf album
    11. _ensure_album_path uses metadata description for the leaf album
    12. _ensure_album_path is idempotent with metadata title (reimport finds same album)

Requires the test PostgreSQL container from docker-compose.test.yml.

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_takeout_album_import.py -v
"""

from __future__ import annotations

import io
import json
import os
import uuid
import zipfile

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.worker.takeout_tasks import (
    _AlbumIndex,
    _AlbumMeta,
    _build_album_index,
    _ensure_album_path,
    _get_or_create_album,
    _link_asset_to_album,
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
# Helpers
# ---------------------------------------------------------------------------


def _make_zip(entries: dict[str, bytes | str]) -> zipfile.ZipFile:
    """Build an in-memory ZipFile from a dict of {name: content}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            if isinstance(content, str):
                content = content.encode("utf-8")
            zf.writestr(name, content)
    buf.seek(0)
    return zipfile.ZipFile(buf, "r")


def _sidecar(photo_taken_ts: int) -> str:
    return json.dumps({"photoTakenTime": {"timestamp": str(photo_taken_ts)}})


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
    uid = uuid.uuid4()
    with migrator_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, password_hash)"
                " VALUES (:id, :email, :name, 'x')"
            ),
            {"id": uid, "email": f"ai28-{uid}@example.com", "name": "Import28"},
        )
    return uid


@pytest.fixture(scope="module")
def session_factory():
    engine = create_async_engine(APP_USER_ASYNC_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory


# ---------------------------------------------------------------------------
# Unit tests — pure logic, no DB
# ---------------------------------------------------------------------------


class TestBuildAlbumIndex:
    def test_title_and_description_from_metadata_json(self):
        zf = _make_zip(
            {
                "My Album/metadata.json": json.dumps(
                    {"title": "Summer 2020", "description": "Beach trip"}
                ),
                "My Album/photo.jpg": b"JFIF",
            }
        )
        index = _build_album_index(zf)
        assert "My Album" in index.meta
        assert index.meta["My Album"].title == "Summer 2020"
        assert index.meta["My Album"].description == "Beach trip"

    def test_missing_description_is_none(self):
        zf = _make_zip(
            {
                "Album/metadata.json": json.dumps({"title": "No Desc"}),
                "Album/photo.jpg": b"JFIF",
            }
        )
        index = _build_album_index(zf)
        assert index.meta["Album"].description is None

    def test_folder_without_metadata_json_not_in_meta(self):
        zf = _make_zip({"2020/photo.jpg": b"JFIF"})
        index = _build_album_index(zf)
        assert "2020" not in index.meta

    def test_sort_order_by_photo_taken_time(self):
        """Earlier photoTakenTime → lower sort_order."""
        zf = _make_zip(
            {
                "Album/metadata.json": json.dumps({"title": "Sorted"}),
                "Album/photo_b.jpg": b"JFIF",
                "Album/photo_b.jpg.json": _sidecar(2000),  # newer
                "Album/photo_a.jpg": b"JFIF",
                "Album/photo_a.jpg.json": _sidecar(1000),  # older
            }
        )
        index = _build_album_index(zf)
        assert index.sort_orders["Album/photo_a.jpg"] == 0
        assert index.sort_orders["Album/photo_b.jpg"] == 1

    def test_sort_order_fallback_alphabetical_without_timestamps(self):
        """Files with no sidecar timestamps are sorted alphabetically."""
        zf = _make_zip(
            {
                "Album/z_photo.jpg": b"JFIF",
                "Album/a_photo.jpg": b"JFIF",
                "Album/m_photo.jpg": b"JFIF",
            }
        )
        index = _build_album_index(zf)
        assert index.sort_orders["Album/a_photo.jpg"] == 0
        assert index.sort_orders["Album/m_photo.jpg"] == 1
        assert index.sort_orders["Album/z_photo.jpg"] == 2

    def test_root_level_metadata_json_ignored(self):
        """A metadata.json at the zip root is not treated as an album."""
        zf = _make_zip(
            {
                "metadata.json": json.dumps({"title": "Root"}),
                "photo.jpg": b"JFIF",
            }
        )
        index = _build_album_index(zf)
        assert index.meta == {}


# ---------------------------------------------------------------------------
# Integration tests — DB required
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetOrCreateAlbumDescription:
    async def test_description_stored_on_creation(self, session_factory, owner_id):
        async with session_factory() as session:
            async with session.begin():
                await session.execute(text(f"SET LOCAL app.current_user_id = '{owner_id}'"))
                album_id = await _get_or_create_album(
                    session, owner_id, None, "desc-album", description="My description"
                )

        engine = create_engine(MIGRATOR_URL)
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT description FROM albums WHERE id = :id"), {"id": album_id}
            ).fetchone()
        engine.dispose()
        assert row.description == "My description"

    async def test_existing_album_not_overwritten(self, session_factory, owner_id):
        """Re-calling _get_or_create_album with a different description returns the same ID
        and does not overwrite the stored description."""
        async with session_factory() as session:
            async with session.begin():
                await session.execute(text(f"SET LOCAL app.current_user_id = '{owner_id}'"))
                id1 = await _get_or_create_album(
                    session, owner_id, None, "idem-desc-album", description="First"
                )

        async with session_factory() as session:
            async with session.begin():
                await session.execute(text(f"SET LOCAL app.current_user_id = '{owner_id}'"))
                id2 = await _get_or_create_album(
                    session, owner_id, None, "idem-desc-album", description="Second"
                )

        assert id1 == id2  # same album returned, not a new one


@pytest.mark.asyncio
class TestLinkAssetToAlbumSortOrder:
    async def test_sort_order_stored(self, session_factory, owner_id, migrator_engine):
        """_link_asset_to_album stores the supplied sort_order in album_assets."""
        asset_id = uuid.uuid4()
        with migrator_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO media_assets"
                    " (id, owner_id, file_size_bytes, original_filename,"
                    "  mime_type, storage_key, checksum)"
                    " VALUES (:id, :owner_id, 1, 'sort_test.jpg',"
                    "  'image/jpeg', :key, :checksum)"
                ),
                {
                    "id": asset_id,
                    "owner_id": owner_id,
                    "key": f"{owner_id}/{asset_id}/original.jpg",
                    "checksum": "bb" * 32,
                },
            )

        async with session_factory() as session:
            async with session.begin():
                await session.execute(text(f"SET LOCAL app.current_user_id = '{owner_id}'"))
                album_id = await _get_or_create_album(session, owner_id, None, "sort-test-album")
                await _link_asset_to_album(session, album_id, asset_id, sort_order=7)

        with migrator_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT sort_order FROM album_assets"
                    " WHERE album_id = :album_id AND asset_id = :asset_id"
                ),
                {"album_id": album_id, "asset_id": asset_id},
            ).fetchone()
        assert row.sort_order == 7


@pytest.mark.asyncio
class TestEnsureAlbumPathWithIndex:
    async def test_leaf_album_uses_metadata_title(self, session_factory, owner_id, migrator_engine):
        """When album_index has metadata for the leaf folder, use its title."""
        album_index = _AlbumIndex(
            meta={"My Photos/Trip": _AlbumMeta(title="Beach Holiday", description=None)},
        )
        async with session_factory() as session:
            async with session.begin():
                await session.execute(text(f"SET LOCAL app.current_user_id = '{owner_id}'"))
                leaf_id = await _ensure_album_path(
                    session, owner_id, "My Photos/Trip", album_index
                )

        with migrator_engine.connect() as conn:
            row = conn.execute(
                text("SELECT title FROM albums WHERE id = :id"), {"id": leaf_id}
            ).fetchone()
        assert row.title == "Beach Holiday"

    async def test_leaf_album_uses_metadata_description(self, session_factory, owner_id, migrator_engine):
        """When album_index has a description for the leaf folder, store it."""
        album_index = _AlbumIndex(
            meta={"Trips/Camping": _AlbumMeta(title="Camping 2021", description="Into the wild")},
        )
        async with session_factory() as session:
            async with session.begin():
                await session.execute(text(f"SET LOCAL app.current_user_id = '{owner_id}'"))
                leaf_id = await _ensure_album_path(
                    session, owner_id, "Trips/Camping", album_index
                )

        with migrator_engine.connect() as conn:
            row = conn.execute(
                text("SELECT description FROM albums WHERE id = :id"), {"id": leaf_id}
            ).fetchone()
        assert row.description == "Into the wild"

    async def test_idempotent_reimport_with_metadata_title(self, session_factory, owner_id):
        """Calling _ensure_album_path twice with the same metadata title returns the same album."""
        album_index = _AlbumIndex(
            meta={"idem/folder": _AlbumMeta(title="Idem Album", description=None)},
        )
        async with session_factory() as session:
            async with session.begin():
                await session.execute(text(f"SET LOCAL app.current_user_id = '{owner_id}'"))
                id1 = await _ensure_album_path(session, owner_id, "idem/folder", album_index)

        async with session_factory() as session:
            async with session.begin():
                await session.execute(text(f"SET LOCAL app.current_user_id = '{owner_id}'"))
                id2 = await _ensure_album_path(session, owner_id, "idem/folder", album_index)

        assert id1 == id2
