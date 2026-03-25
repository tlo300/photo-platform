"""Add parent_id to albums for nested hierarchy (issue #75).

Adds a self-referential parent_id column so albums can be nested.
Two partial unique indexes enforce idempotent album lookup during import:
  - root albums: UNIQUE (owner_id, title) WHERE parent_id IS NULL
  - nested albums: UNIQUE (owner_id, parent_id, title) WHERE parent_id IS NOT NULL

Revision ID: 0013
Revises: 0012
Create Date: 2026-03-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "albums",
        sa.Column(
            "parent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("albums.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    # Partial unique index for root albums (parent_id IS NULL)
    op.create_index(
        "ix_albums_owner_title_root",
        "albums",
        ["owner_id", "title"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NULL"),
    )
    # Partial unique index for nested albums (parent_id IS NOT NULL)
    op.create_index(
        "ix_albums_owner_parent_title",
        "albums",
        ["owner_id", "parent_id", "title"],
        unique=True,
        postgresql_where=sa.text("parent_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_albums_owner_parent_title", table_name="albums")
    op.drop_index("ix_albums_owner_title_root", table_name="albums")
    op.drop_column("albums", "parent_id")
