"""Add upload_keys and target_album_id to import_jobs for direct upload (issue #91).

Revision ID: 0020
Revises: 0019
Create Date: 2026-03-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "import_jobs",
        sa.Column("upload_keys", JSONB, nullable=True),
    )
    op.add_column(
        "import_jobs",
        sa.Column(
            "target_album_id",
            UUID(as_uuid=True),
            sa.ForeignKey("albums.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("import_jobs", "target_album_id")
    op.drop_column("import_jobs", "upload_keys")
