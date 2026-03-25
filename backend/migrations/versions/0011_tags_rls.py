"""Add Row-Level Security to the tags table.

The tags table has an owner_id column but was omitted from the initial RLS
migration (0003).  Without a policy, app_user can read tag names belonging to
other users, which would expose people names imported from their Google Takeout
sidecars.

This migration adds the same owner_isolation policy used by media_assets so
that every query against tags is automatically filtered to the current user.

Revision ID: 0011
Revises: 0010
Create Date: 2026-03-25
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SETTING = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"


def upgrade() -> None:
    op.execute("ALTER TABLE tags ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY owner_isolation ON tags
          USING      (owner_id = {_SETTING})
          WITH CHECK (owner_id = {_SETTING})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS owner_isolation ON tags")
    op.execute("ALTER TABLE tags DISABLE ROW LEVEL SECURITY")
