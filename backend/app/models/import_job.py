"""SQLAlchemy model for tracking import job progress (Takeout and direct upload)."""

import enum
import uuid

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ImportJobStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    done = "done"
    failed = "failed"


class ImportJob(Base):
    __tablename__ = "import_jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    status: Mapped[ImportJobStatus] = mapped_column(
        Enum(ImportJobStatus, name="import_job_status"),
        nullable=False,
        server_default="pending",
    )
    zip_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    folder_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    duplicates: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    no_sidecar: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    errors: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # Direct upload fields (issue #91)
    upload_keys: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    target_album_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("albums.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
