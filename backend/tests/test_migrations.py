"""Integration tests for the Alembic migration.

Requires the test Postgres container from docker-compose.test.yml to be running
(pg on localhost:5433, db=photo_test, superuser/superpassword).

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_migrations.py -v
"""
import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


# ---------------------------------------------------------------------------
# Connection URLs — use env vars so CI can override them
# ---------------------------------------------------------------------------
MIGRATOR_URL = os.environ.get(
    "TEST_DATABASE_MIGRATOR_URL",
    "postgresql+psycopg2://migrator:testpassword@localhost:5433/photo_test",
)
APP_USER_URL = os.environ.get(
    "TEST_DATABASE_APP_URL",
    "postgresql+psycopg2://app_user:testpassword@localhost:5433/photo_test",
)


def alembic_cfg() -> Config:
    cfg = Config()
    # alembic.ini lives one directory above the tests/ folder (i.e. backend/)
    ini_path = os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
    cfg.config_file_name = os.path.abspath(ini_path)
    cfg.set_main_option("sqlalchemy.url", MIGRATOR_URL)
    # Point script_location to the actual migrations folder
    migrations_path = os.path.join(os.path.dirname(__file__), "..", "migrations")
    cfg.set_main_option("script_location", os.path.abspath(migrations_path))
    return cfg


@pytest.fixture(scope="module", autouse=True)
def run_migrations():
    """Upgrade to head before tests, downgrade to base after."""
    cfg = alembic_cfg()
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


@pytest.fixture(scope="module")
def engine():
    e = create_engine(MIGRATOR_URL)
    yield e
    e.dispose()


# ---------------------------------------------------------------------------
# Schema structure tests
# ---------------------------------------------------------------------------

EXPECTED_TABLES = {
    "users",
    "media_assets",
    "media_metadata",
    "locations",
    "albums",
    "album_assets",
    "tags",
    "asset_tags",
    "security_events",
    "shares",
    "invitations",
}


def test_all_tables_created(engine):
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert EXPECTED_TABLES.issubset(tables)


def test_users_columns(engine):
    insp = inspect(engine)
    cols = {c["name"] for c in insp.get_columns("users")}
    assert {"id", "email", "display_name", "role", "storage_used_bytes", "created_at", "suspended_at"}.issubset(cols)


def test_locations_point_is_geometry(engine):
    insp = inspect(engine)
    cols = {c["name"]: c for c in insp.get_columns("locations")}
    assert "point" in cols
    # GeoAlchemy2 columns show up as 'geometry' type
    assert "geometry" in str(cols["point"]["type"]).lower()


def test_gist_index_on_locations_point(engine):
    insp = inspect(engine)
    indexes = {idx["name"] for idx in insp.get_indexes("locations")}
    assert "ix_locations_point" in indexes


def test_btree_index_on_media_assets_captured_at(engine):
    insp = inspect(engine)
    indexes = {idx["name"] for idx in insp.get_indexes("media_assets")}
    assert "ix_media_assets_captured_at" in indexes


def test_composite_index_on_media_assets_owner_captured(engine):
    insp = inspect(engine)
    indexes = {idx["name"] for idx in insp.get_indexes("media_assets")}
    assert "ix_media_assets_owner_captured" in indexes


# ---------------------------------------------------------------------------
# Role tests
# ---------------------------------------------------------------------------

def test_app_user_role_exists(engine):
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = 'app_user'")
        )
        assert result.fetchone() is not None


def test_migrator_role_exists(engine):
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = 'migrator'")
        )
        assert result.fetchone() is not None


def test_app_user_can_dml(engine):
    """app_user must be able to SELECT/INSERT/UPDATE/DELETE on core tables."""
    app_engine = create_engine(APP_USER_URL)
    try:
        with app_engine.connect() as conn:
            # INSERT a row
            conn.execute(
                text(
                    "INSERT INTO users (email, display_name) VALUES (:e, :n)"
                ),
                {"e": "test@example.com", "n": "Test User"},
            )
            # SELECT it back
            row = conn.execute(
                text("SELECT email FROM users WHERE email = :e"),
                {"e": "test@example.com"},
            ).fetchone()
            assert row is not None
            assert row[0] == "test@example.com"
            # UPDATE
            conn.execute(
                text("UPDATE users SET display_name = :n WHERE email = :e"),
                {"n": "Updated", "e": "test@example.com"},
            )
            # DELETE
            conn.execute(
                text("DELETE FROM users WHERE email = :e"),
                {"e": "test@example.com"},
            )
            conn.commit()
    finally:
        app_engine.dispose()


def test_app_user_cannot_create_table(engine):
    """app_user must NOT be able to create tables (no schema CREATE privilege)."""
    app_engine = create_engine(APP_USER_URL)
    try:
        with app_engine.connect() as conn:
            with pytest.raises(Exception, match="permission denied"):
                conn.execute(text("CREATE TABLE _forbidden (id int)"))
                conn.commit()
    finally:
        app_engine.dispose()
