"""Row-Level Security policies for all user-owned tables.

Enables RLS on media_assets, media_metadata, locations, albums,
album_assets, and asset_tags.  All policies filter by
current_setting('app.current_user_id', true)::uuid so that:

  - Rows are visible only to the owning user.
  - When no session variable is set, current_setting returns '' (empty
    string); NULLIF converts that to NULL so every owner_id comparison
    evaluates to NULL/false and 0 rows are returned.

FastAPI sets the session variable via SET LOCAL in get_authed_session()
(app/db.py) at the start of every authenticated transaction.

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-24
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_USER = "app_user"

# Tables with a direct owner_id column.
_DIRECT_OWNER_TABLES = ["media_assets", "albums"]

# Tables whose ownership is resolved through a parent table.
# Tuple: (table, join_column, parent_table, parent_pk, parent_owner_col)
_INDIRECT_OWNER_TABLES = [
    ("media_metadata", "asset_id", "media_assets", "id", "owner_id"),
    ("locations",      "asset_id", "media_assets", "id", "owner_id"),
    ("album_assets",   "album_id", "albums",        "id", "owner_id"),
    ("asset_tags",     "asset_id", "media_assets",  "id", "owner_id"),
]

_SETTING = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Tables with a direct owner_id column
    # ------------------------------------------------------------------
    for table in _DIRECT_OWNER_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY owner_isolation ON {table}
              USING      (owner_id = {_SETTING})
              WITH CHECK (owner_id = {_SETTING})
            """
        )

    # ------------------------------------------------------------------
    # Tables whose ownership is resolved via a parent table
    # ------------------------------------------------------------------
    for table, join_col, parent, parent_pk, owner_col in _INDIRECT_OWNER_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY owner_isolation ON {table}
              USING (
                EXISTS (
                  SELECT 1 FROM {parent}
                   WHERE {parent}.{parent_pk} = {table}.{join_col}
                     AND {parent}.{owner_col} = {_SETTING}
                )
              )
            """
        )

    # ------------------------------------------------------------------
    # Revoke DDL privileges from app_user
    #
    # app_user does not own any tables so ALTER TABLE / DROP TABLE /
    # DISABLE TRIGGER are already blocked by Postgres ownership checks.
    # The explicit REVOKE here removes the last foothold: the ability to
    # create new objects in the public schema.
    # ------------------------------------------------------------------
    op.execute(f"REVOKE CREATE ON SCHEMA public FROM {APP_USER}")


def downgrade() -> None:
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_USER}")

    for table, _, _, _, _ in reversed(_INDIRECT_OWNER_TABLES):
        op.execute(f"DROP POLICY IF EXISTS owner_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    for table in reversed(_DIRECT_OWNER_TABLES):
        op.execute(f"DROP POLICY IF EXISTS owner_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
