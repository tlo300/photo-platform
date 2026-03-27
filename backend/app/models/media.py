import uuid

from geoalchemy2 import Geometry
from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MediaAsset(Base):
    __tablename__ = "media_assets"
    __table_args__ = (
        Index("ix_media_assets_captured_at", "captured_at"),
        Index("ix_media_assets_owner_captured", "owner_id", "captured_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    captured_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(127), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    sidecar_missing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    thumbnail_ready: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    thumbnail_error: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_live_photo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    live_video_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class MediaMetadata(Base):
    __tablename__ = "media_metadata"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("media_assets.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    make: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    width_px: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height_px: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    iso: Mapped[int | None] = mapped_column(Integer, nullable=True)
    aperture: Mapped[float | None] = mapped_column(Float, nullable=True)
    shutter_speed: Mapped[float | None] = mapped_column(Float, nullable=True)
    focal_length: Mapped[float | None] = mapped_column(Float, nullable=True)
    flash: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class Location(Base):
    __tablename__ = "locations"
    __table_args__ = (
        Index("ix_locations_point", "point", postgresql_using="gist"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("media_assets.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    point: Mapped[object] = mapped_column(Geometry("POINT", srid=4326), nullable=False)
    accuracy_metres: Mapped[float | None] = mapped_column(Float, nullable=True)
    altitude_metres: Mapped[float | None] = mapped_column(Float, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    country: Mapped[str | None] = mapped_column(String(128), nullable=True)
