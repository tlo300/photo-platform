"""Sharing models: Share (link/user/album shares) and Invitation (admin-issued registration tokens)."""

import enum
import uuid

from sqlalchemy import DateTime, Enum, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ShareType(str, enum.Enum):
    link = "link"
    user = "user"
    album = "album"


class SharePermission(str, enum.Enum):
    view = "view"
    contribute = "contribute"


class Share(Base):
    """A share record linking a target (asset or album) to an access grant.

    share_type determines who can access and what target_id refers to:
      - link  : anyone with the URL; target_id = media_asset UUID
      - user  : a specific registered user; target_id = media_asset UUID,
                shared_with_user_id must be set
      - album : an entire album; target_id = album UUID,
                shared_with_user_id optional (if set, restricted to that user)

    The raw share token is never stored; only its SHA-256 hex digest (token_hash).
    """

    __tablename__ = "shares"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    share_type: Mapped[ShareType] = mapped_column(
        Enum(ShareType, name="share_type", create_constraint=False),
        nullable=False,
    )
    # Polymorphic reference — points to a media_asset or album UUID.
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    shared_with_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    permission: Mapped[SharePermission] = mapped_column(
        Enum(SharePermission, name="share_permission", create_constraint=False),
        nullable=False,
        server_default="view",
    )
    expires_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    password_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    revoked_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    accepted_at: Mapped[DateTime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
