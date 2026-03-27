"""Add live photo columns to media_assets.

Revision ID: 0022
Revises: 0021
Create Date: 2026-03-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "media_assets",
        sa.Column("is_live_photo", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "media_assets",
        sa.Column("live_video_key", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("media_assets", "live_video_key")
    op.drop_column("media_assets", "is_live_photo")
