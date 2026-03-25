"""Add sort_order to album_assets for reorderable asset lists (issue #27).

Adds an INTEGER sort_order column (default 0) to album_assets so assets
within an album can be ordered and reordered by the Albums API.

Revision ID: 0018
Revises: 0017
Create Date: 2026-03-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "album_assets",
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_album_assets_album_sort",
        "album_assets",
        ["album_id", "sort_order"],
    )


def downgrade() -> None:
    op.drop_index("ix_album_assets_album_sort", table_name="album_assets")
    op.drop_column("album_assets", "sort_order")
