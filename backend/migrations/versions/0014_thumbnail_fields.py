"""Add thumbnail_ready and thumbnail_error flags to media_assets (issue #23).

thumbnail_ready is set to true by the thumbnail worker on success.
thumbnail_error is set to true after all retries are exhausted.

Revision ID: 0014
Revises: 0013
Create Date: 2026-03-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "media_assets",
        sa.Column(
            "thumbnail_ready",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "media_assets",
        sa.Column(
            "thumbnail_error",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("media_assets", "thumbnail_error")
    op.drop_column("media_assets", "thumbnail_ready")
