"""Add sidecar_missing flag to media_assets and no_sidecar counter to import_jobs.

Tracks assets ingested without a Google Takeout JSON sidecar (issue #41).

Revision ID: 0010
Revises: 0009
Create Date: 2026-03-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "media_assets",
        sa.Column(
            "sidecar_missing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "import_jobs",
        sa.Column(
            "no_sidecar",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("import_jobs", "no_sidecar")
    op.drop_column("media_assets", "sidecar_missing")
