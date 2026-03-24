"""invitation tokens: rename token -> token_hash on invitations table

The initial schema stored the raw token in the `token` column.  This migration
renames it to `token_hash` to match the convention used by refresh_tokens and
password reset tokens — only the SHA-256 hash is ever stored in the database.

Revision ID: 0006
Revises: 0005
Create Date: 2026-03-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("invitations", "token", new_column_name="token_hash")


def downgrade() -> None:
    op.alter_column("invitations", "token_hash", new_column_name="token")
