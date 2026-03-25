"""Add GIN full-text search index on media_metadata for camera make/model (issue #92).

Adds a functional GIN index on media_metadata(make, model) so that the
GET /assets/search?q= endpoint can match against camera manufacturer and
model name without a full table scan.

Uses the 'simple' dictionary — no stemming, preserves brand names and
model identifiers (e.g. "Sony", "A7III", "Canon EOS R5").

Revision ID: 0017
Revises: 0016
Create Date: 2026-03-25
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Camera make + model combined (simple — preserves brand names and model codes)
    op.execute(
        "CREATE INDEX ix_media_metadata_camera_fts "
        "ON media_metadata USING gin("
        "to_tsvector('simple', coalesce(make, '') || ' ' || coalesce(model, '')))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_media_metadata_camera_fts")
