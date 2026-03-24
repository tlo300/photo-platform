"""admin user management: password reset token fields on users

Adds password_reset_token_hash and password_reset_token_expires_at to the
users table to support the admin-generated one-time reset token flow.

The raw token is returned to the admin and passed to the user out-of-band.
Only the SHA-256 hash is stored.  A future auth endpoint will consume it.

Revision ID: 0005
Revises: 0004
Create Date: 2026-03-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_reset_token_hash", sa.String(255), nullable=True))
    op.add_column(
        "users",
        sa.Column("password_reset_token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "password_reset_token_expires_at")
    op.drop_column("users", "password_reset_token_hash")
