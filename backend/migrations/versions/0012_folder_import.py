"""Add folder_path column and make zip_key nullable for local folder imports.

Revision ID: 0012
Revises: 0011
Create Date: 2026-03-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("import_jobs", "zip_key", nullable=True)
    op.add_column(
        "import_jobs",
        sa.Column("folder_path", sa.String(1024), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("import_jobs", "folder_path")
    # Set any null zip_keys (from folder jobs) to empty string before restoring NOT NULL
    op.execute("UPDATE import_jobs SET zip_key = '' WHERE zip_key IS NULL")
    op.alter_column("import_jobs", "zip_key", nullable=False)
