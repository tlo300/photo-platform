"""Security audit log: add user_agent, rename detail→metadata, RLS immutability.

Adds the missing user_agent column and renames the JSONB payload column from
detail to metadata to match the acceptance criteria in issue #13.

RLS policy:
  - app_user may INSERT and SELECT rows.
  - No UPDATE or DELETE policy is created, so those operations are blocked by
    Postgres for app_user.  The migrator (table owner / superuser) bypasses RLS
    by default and retains full access for schema migrations.

Revision ID: 0004
Revises: 0003
Create Date: 2026-03-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_USER = "app_user"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Add user_agent column
    # ------------------------------------------------------------------
    op.add_column(
        "security_events",
        sa.Column("user_agent", sa.String(512), nullable=True),
    )

    # ------------------------------------------------------------------
    # Rename detail → metadata
    # ------------------------------------------------------------------
    op.alter_column("security_events", "detail", new_column_name="metadata")

    # ------------------------------------------------------------------
    # Index for common query patterns (filter by user, filter by type)
    # ------------------------------------------------------------------
    op.create_index("ix_security_events_user_id", "security_events", ["user_id"])
    op.create_index("ix_security_events_event_type", "security_events", ["event_type"])
    op.create_index("ix_security_events_created_at", "security_events", ["created_at"])

    # ------------------------------------------------------------------
    # Revoke UPDATE and DELETE from app_user — immutability at privilege level
    # ------------------------------------------------------------------
    op.execute(f"REVOKE UPDATE, DELETE ON TABLE security_events FROM {APP_USER}")

    # ------------------------------------------------------------------
    # RLS — enable and add INSERT + SELECT policies for app_user
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE security_events ENABLE ROW LEVEL SECURITY")

    # Any connected role may insert (auth endpoints run as app_user, no
    # per-row ownership concept needed for audit events).
    op.execute(
        """
        CREATE POLICY se_insert ON security_events
          FOR INSERT
          WITH CHECK (true)
        """
    )

    # Any connected role may select (admin endpoint does Python-level
    # role check; DB level allows the query through).
    op.execute(
        """
        CREATE POLICY se_select ON security_events
          FOR SELECT
          USING (true)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS se_select ON security_events")
    op.execute("DROP POLICY IF EXISTS se_insert ON security_events")
    op.execute("ALTER TABLE security_events DISABLE ROW LEVEL SECURITY")

    op.execute(f"GRANT UPDATE, DELETE ON TABLE security_events TO {APP_USER}")

    op.drop_index("ix_security_events_created_at", table_name="security_events")
    op.drop_index("ix_security_events_event_type", table_name="security_events")
    op.drop_index("ix_security_events_user_id", table_name="security_events")

    op.alter_column("security_events", "metadata", new_column_name="detail")
    op.drop_column("security_events", "user_agent")
