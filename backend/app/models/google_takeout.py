"""SQLAlchemy model for verbatim Google Takeout sidecar JSON storage."""

import uuid

from sqlalchemy import ForeignKey, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class GoogleMetadataRaw(Base):
    __tablename__ = "google_metadata_raw"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("media_assets.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    raw_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
