"""Shares table v2 – full sharing data model.

Replaces the stub shares table from 0001 with the complete schema:
  share_type (link|user|album), target_id, shared_with_user_id,
  token_hash, permission (view|contribute), password_hash, revoked_at.

RLS policies:
  - owner_isolation  : owner can INSERT/UPDATE/SELECT/DELETE their own rows
  - link_read_access : allow unauthenticated SELECT on link-type shares so
                       GET /shares/{token} can resolve tokens without a
                       logged-in user (app.current_user_id not set).

Revision ID: 0007
Revises: 0006
Create Date: 2026-03-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_USER = "app_user"
_SETTING = "NULLIF(current_setting('app.current_user_id', true), '')::uuid"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Remove the stub shares table created in 0001.
    # Drop the table before enum types: Postgres refuses to drop a type
    # that is still referenced by a column, even with IF EXISTS.
    # ------------------------------------------------------------------
    op.execute(f"REVOKE ALL ON TABLE shares FROM {APP_USER}")
    op.drop_table("shares")

    # ------------------------------------------------------------------
    # Drop enum types that may exist from a previous partial run.
    # Safe now that the table is gone.
    # ------------------------------------------------------------------
    op.execute("DROP TYPE IF EXISTS share_type")
    op.execute("DROP TYPE IF EXISTS share_permission")

    # ------------------------------------------------------------------
    # Enum types and new shares table — use raw SQL to avoid SQLAlchemy
    # re-emitting CREATE TYPE inside op.create_table even with create_type=False.
    # ------------------------------------------------------------------
    op.execute("CREATE TYPE share_type AS ENUM ('link', 'user', 'album')")
    op.execute("CREATE TYPE share_permission AS ENUM ('view', 'contribute')")

    op.execute(
        f"""
        CREATE TABLE shares (
            id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id        UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            share_type      share_type  NOT NULL,
            target_id       UUID        NOT NULL,
            shared_with_user_id UUID    REFERENCES users(id) ON DELETE SET NULL,
            token_hash      VARCHAR(64) NOT NULL UNIQUE,
            permission      share_permission NOT NULL DEFAULT 'view',
            expires_at      TIMESTAMPTZ,
            password_hash   VARCHAR(128),
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            revoked_at      TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX ix_shares_owner_id   ON shares (owner_id)")
    op.execute("CREATE INDEX ix_shares_token_hash ON shares (token_hash)")

    # Restore DML grant for app_user
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE shares TO {APP_USER}")

    # ------------------------------------------------------------------
    # RLS
    # ------------------------------------------------------------------
    op.execute("ALTER TABLE shares ENABLE ROW LEVEL SECURITY")

    # Owners can do everything with their own shares.
    op.execute(
        f"""
        CREATE POLICY owner_isolation ON shares
          USING      (owner_id = {_SETTING})
          WITH CHECK (owner_id = {_SETTING})
        """
    )

    # Unauthenticated GET /shares/{{token}} needs to SELECT link-type rows
    # without a current_user_id set.  This permissive policy lets that through
    # while the owner_isolation policy still allows owners to manage all types.
    op.execute(
        """
        CREATE POLICY link_read_access ON shares
          FOR SELECT
          USING (share_type = 'link')
        """
    )


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON TABLE shares FROM {APP_USER}")
    op.drop_index("ix_shares_token_hash", table_name="shares")
    op.drop_index("ix_shares_owner_id", table_name="shares")
    op.drop_table("shares")
    op.execute("DROP TYPE IF EXISTS share_permission")
    op.execute("DROP TYPE IF EXISTS share_type")

    # Recreate stub shares table from 0001
    op.create_table(
        "shares",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("album_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("token", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["album_id"], ["albums.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["media_assets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token"),
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE shares TO {APP_USER}")
