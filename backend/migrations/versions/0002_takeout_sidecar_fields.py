"""takeout sidecar fields

Adds columns and a table needed for the Google Takeout sidecar parser:
  - media_assets.description     (TEXT, nullable)
  - locations.altitude_metres    (FLOAT, nullable)
  - asset_tags.source            (VARCHAR(64), nullable)
  - google_metadata_raw          (new table: id, asset_id FK, raw_json JSONB)

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

APP_USER = "app_user"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # media_assets: free-text description from the sidecar
    # ------------------------------------------------------------------
    op.add_column("media_assets", sa.Column("description", sa.Text(), nullable=True))

    # ------------------------------------------------------------------
    # locations: altitude from geoData
    # ------------------------------------------------------------------
    op.add_column(
        "locations", sa.Column("altitude_metres", sa.Float(), nullable=True)
    )

    # ------------------------------------------------------------------
    # asset_tags: optional source label (e.g. 'google_people')
    # ------------------------------------------------------------------
    op.add_column(
        "asset_tags", sa.Column("source", sa.String(64), nullable=True)
    )

    # ------------------------------------------------------------------
    # google_metadata_raw: verbatim sidecar JSON, one row per asset
    # ------------------------------------------------------------------
    op.create_table(
        "google_metadata_raw",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("asset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("raw_json", postgresql.JSONB(), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["media_assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_id"),
    )

    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE google_metadata_raw TO {APP_USER}"
    )


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON TABLE google_metadata_raw FROM {APP_USER}")
    op.drop_table("google_metadata_raw")
    op.drop_column("asset_tags", "source")
    op.drop_column("locations", "altitude_metres")
    op.drop_column("media_assets", "description")
