"""Integration tests for Row-Level Security policies.

Verifies the two critical RLS behaviours:

  1. Authenticated as user A, a query for user B's asset returns 0 rows
     (not a 403 — the row is silently invisible).
  2. When no session variable is set, all RLS-protected tables return 0 rows.

Requires the test Postgres container from docker-compose.test.yml to be running
(pg on localhost:5433, db=photo_test, superuser/superpassword).

Run with:
    docker compose -f docker-compose.test.yml up -d
    cd backend && pytest tests/test_rls.py -v
"""

import os
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

MIGRATOR_URL = os.environ.get(
    "TEST_DATABASE_MIGRATOR_URL",
    "postgresql+psycopg://migrator:testpassword@localhost:5433/photo_test",
)
APP_USER_URL = os.environ.get(
    "TEST_DATABASE_APP_URL",
    "postgresql+psycopg://app_user:testpassword@localhost:5433/photo_test",
)


def alembic_cfg() -> Config:
    cfg = Config()
    ini_path = os.path.join(os.path.dirname(__file__), "..", "alembic.ini")
    cfg.config_file_name = os.path.abspath(ini_path)
    cfg.set_main_option("sqlalchemy.url", MIGRATOR_URL)
    migrations_path = os.path.join(os.path.dirname(__file__), "..", "migrations")
    cfg.set_main_option("script_location", os.path.abspath(migrations_path))
    return cfg


@pytest.fixture(scope="module", autouse=True)
def run_migrations():
    cfg = alembic_cfg()
    command.upgrade(cfg, "head")
    yield
    command.downgrade(cfg, "base")


@pytest.fixture(scope="module")
def migrator_engine():
    e = create_engine(MIGRATOR_URL)
    yield e
    e.dispose()


@pytest.fixture(scope="module")
def app_engine():
    e = create_engine(APP_USER_URL)
    yield e
    e.dispose()


@pytest.fixture(scope="module")
def two_users_one_asset(migrator_engine):
    """Seed user_a, user_b, and one media_asset owned by user_b.

    Returns (user_a_id, user_b_id, asset_id).
    Rows are cleaned up after the module.
    """
    user_a_id = str(uuid.uuid4())
    user_b_id = str(uuid.uuid4())
    asset_id = str(uuid.uuid4())

    with migrator_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email, display_name) VALUES "
                "(:a_id, :a_email, 'User A'), "
                "(:b_id, :b_email, 'User B')"
            ),
            {
                "a_id": user_a_id,
                "a_email": f"user-a-{user_a_id}@example.com",
                "b_id": user_b_id,
                "b_email": f"user-b-{user_b_id}@example.com",
            },
        )
        conn.execute(
            text(
                "INSERT INTO media_assets "
                "(id, owner_id, file_size_bytes, original_filename, mime_type, storage_key, checksum) "
                "VALUES (:id, :owner, 1024, 'photo.jpg', 'image/jpeg', :key, 'abc123')"
            ),
            {
                "id": asset_id,
                "owner": user_b_id,
                "key": f"{user_b_id}/{asset_id}/original.jpg",
            },
        )

    yield user_a_id, user_b_id, asset_id

    with migrator_engine.begin() as conn:
        conn.execute(text("DELETE FROM media_assets WHERE id = :id"), {"id": asset_id})
        conn.execute(
            text("DELETE FROM users WHERE id IN (:a, :b)"),
            {"a": user_a_id, "b": user_b_id},
        )


# ---------------------------------------------------------------------------
# RLS tests
# ---------------------------------------------------------------------------


def test_user_a_cannot_see_user_b_asset(app_engine, two_users_one_asset):
    """Authenticated as user A, user B's asset is invisible (0 rows, not 403)."""
    user_a_id, _user_b_id, asset_id = two_users_one_asset

    with app_engine.connect() as conn:
        conn.execute(text("BEGIN"))
        conn.execute(
            text(f"SET LOCAL app.current_user_id = '{user_a_id}'")
        )
        rows = conn.execute(
            text("SELECT id FROM media_assets WHERE id = :id"),
            {"id": asset_id},
        ).fetchall()
        conn.execute(text("ROLLBACK"))

    assert rows == [], (
        f"Expected 0 rows for user A querying user B's asset, got {len(rows)}"
    )


def test_no_session_variable_returns_zero_rows(app_engine, two_users_one_asset):
    """Without app.current_user_id set, all RLS-protected tables return 0 rows."""
    _user_a_id, _user_b_id, asset_id = two_users_one_asset

    rls_tables = [
        "media_assets",
        "albums",
    ]

    with app_engine.connect() as conn:
        # Do NOT set app.current_user_id — RLS should block everything
        for table in rls_tables:
            rows = conn.execute(text(f"SELECT id FROM {table}")).fetchall()
            assert rows == [], (
                f"Expected 0 rows from {table} with no session variable, got {len(rows)}"
            )

        # Verify the specific asset seeded for user B is also invisible
        rows = conn.execute(
            text("SELECT id FROM media_assets WHERE id = :id"),
            {"id": asset_id},
        ).fetchall()
        assert rows == [], "Seeded asset must not be visible without session variable"


def test_user_b_can_see_own_asset(app_engine, two_users_one_asset):
    """Sanity check: the owning user can read their own asset."""
    _user_a_id, user_b_id, asset_id = two_users_one_asset

    with app_engine.connect() as conn:
        conn.execute(text("BEGIN"))
        conn.execute(
            text(f"SET LOCAL app.current_user_id = '{user_b_id}'")
        )
        rows = conn.execute(
            text("SELECT id FROM media_assets WHERE id = :id"),
            {"id": asset_id},
        ).fetchall()
        conn.execute(text("ROLLBACK"))

    assert len(rows) == 1, (
        f"Expected owner to see their own asset, got {len(rows)} rows"
    )


def test_app_user_cannot_alter_table(app_engine):
    """app_user must not be able to ALTER TABLE on any user-owned table."""
    with app_engine.connect() as conn:
        with pytest.raises(Exception, match="permission denied|must be owner"):
            conn.execute(
                text("ALTER TABLE media_assets ADD COLUMN _forbidden int")
            )
            conn.commit()


def test_app_user_cannot_drop_table(app_engine):
    """app_user must not be able to DROP a user-owned table."""
    with app_engine.connect() as conn:
        with pytest.raises(Exception, match="permission denied|must be owner"):
            conn.execute(text("DROP TABLE media_assets"))
            conn.commit()


def test_app_user_cannot_disable_rls(app_engine):
    """app_user must not be able to disable RLS on a protected table."""
    with app_engine.connect() as conn:
        with pytest.raises(Exception, match="permission denied|must be owner"):
            conn.execute(
                text("ALTER TABLE media_assets DISABLE ROW LEVEL SECURITY")
            )
            conn.commit()
