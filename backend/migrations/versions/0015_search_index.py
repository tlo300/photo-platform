"""Add GIN full-text search indexes for basic search (issue #26).

Adds functional GIN indexes on:
  - media_assets.description  (english stemming)
  - tags.name                 (simple — preserves proper nouns)
  - locations (display_name + country concatenated, simple)

These are used by GET /assets/search?q= to run efficient full-text
queries across all three fields without storing a separate tsvector column.

Revision ID: 0015
Revises: 0014
Create Date: 2026-03-25
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Description full-text index (english — benefits from stemming on prose)
    op.execute(
        "CREATE INDEX ix_media_assets_description_fts "
        "ON media_assets USING gin(to_tsvector('english', coalesce(description, '')))"
    )
    # Tag name index (simple — no stemming; preserves person names and place names)
    op.execute(
        "CREATE INDEX ix_tags_name_fts "
        "ON tags USING gin(to_tsvector('simple', name))"
    )
    # Locality index: display_name + country combined (simple — place names)
    op.execute(
        "CREATE INDEX ix_locations_locality_fts "
        "ON locations USING gin("
        "to_tsvector('simple', coalesce(display_name, '') || ' ' || coalesce(country, '')))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_locations_locality_fts")
    op.execute("DROP INDEX IF EXISTS ix_tags_name_fts")
    op.execute("DROP INDEX IF EXISTS ix_media_assets_description_fts")
