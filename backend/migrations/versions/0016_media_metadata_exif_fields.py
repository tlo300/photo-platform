"""Add extended EXIF and video metadata fields to media_metadata (issue #88).

Adds five new nullable columns to media_metadata:
  iso            — ISO speed rating (integer)
  aperture       — f-number as decimal (float)
  shutter_speed  — exposure time in seconds (float)
  focal_length   — focal length in mm (float)
  flash          — whether the flash fired (boolean)

Video duration (duration_seconds) and image/video dimensions (width_px,
height_px) already exist in the schema and will be back-filled by the
metadata.backfill_asset Celery task.

Revision ID: 0016
Revises: 0015
Create Date: 2026-03-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("media_metadata", sa.Column("iso", sa.Integer(), nullable=True))
    op.add_column("media_metadata", sa.Column("aperture", sa.Float(), nullable=True))
    op.add_column("media_metadata", sa.Column("shutter_speed", sa.Float(), nullable=True))
    op.add_column("media_metadata", sa.Column("focal_length", sa.Float(), nullable=True))
    op.add_column("media_metadata", sa.Column("flash", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("media_metadata", "flash")
    op.drop_column("media_metadata", "focal_length")
    op.drop_column("media_metadata", "shutter_speed")
    op.drop_column("media_metadata", "aperture")
    op.drop_column("media_metadata", "iso")
