"""Media asset service — uploads, deletes, and storage accounting.

All storage key operations delegate to StorageService.  This layer adds the
database side: inserting/removing MediaAsset rows and keeping
users.storage_used_bytes in sync within the same transaction.

The caller owns the session lifecycle (begin / commit / rollback).  This
service only flushes — it never commits — so it can be composed safely with
other operations inside a single request transaction.
"""

import contextlib
import logging
import uuid
from datetime import datetime
from typing import BinaryIO

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.media import MediaAsset
from app.services.storage import StorageError, StorageService, storage_service

logger = logging.getLogger(__name__)


class AssetNotFoundError(Exception):
    """Raised when the requested asset does not exist (or is not visible via RLS)."""


class MediaService:
    """Coordinates S3 uploads/deletes with the corresponding database changes."""

    def __init__(self, storage: StorageService) -> None:
        self._storage = storage

    async def create_asset(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        asset_id: uuid.UUID,
        file_obj: BinaryIO,
        suffix: str,
        content_type: str,
        file_size_bytes: int,
        original_filename: str,
        mime_type: str,
        checksum: str,
        captured_at: datetime | None = None,
    ) -> MediaAsset:
        """Upload *file_obj* to S3 and persist a MediaAsset row.

        ``users.storage_used_bytes`` is incremented by *file_size_bytes* in the
        same flush as the INSERT so both changes either commit or roll back
        together.  If the S3 upload itself fails the database is never touched.
        If the DB flush fails the orphaned S3 object is deleted before re-raising.
        """
        key = self._storage.upload(
            str(user_id), str(asset_id), file_obj, suffix, content_type
        )
        try:
            asset = MediaAsset(
                id=asset_id,
                owner_id=user_id,
                captured_at=captured_at,
                file_size_bytes=file_size_bytes,
                original_filename=original_filename,
                mime_type=mime_type,
                storage_key=key,
                checksum=checksum,
            )
            session.add(asset)
            await session.execute(
                text(
                    "UPDATE users"
                    " SET storage_used_bytes = storage_used_bytes + :delta"
                    " WHERE id = :uid"
                ),
                {"delta": file_size_bytes, "uid": user_id},
            )
            await session.flush()
        except Exception:
            with contextlib.suppress(StorageError):
                self._storage.delete(key)
            raise
        return asset

    async def delete_asset(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        asset_id: uuid.UUID,
    ) -> None:
        """Remove a MediaAsset row and decrement the owner's storage quota.

        The DB row is deleted and ``users.storage_used_bytes`` is decremented in
        the same flush.  The S3 object is removed after the flush succeeds; if
        the S3 delete fails a warning is logged but the error is not re-raised —
        the DB record is the source of truth for ownership and quota.
        """
        asset = await session.get(MediaAsset, asset_id)
        if asset is None:
            raise AssetNotFoundError(f"Asset {asset_id!r} not found")

        key = asset.storage_key
        file_size = asset.file_size_bytes

        await session.delete(asset)
        await session.execute(
            text(
                "UPDATE users"
                " SET storage_used_bytes = GREATEST(0, storage_used_bytes - :delta)"
                " WHERE id = :uid"
            ),
            {"delta": file_size, "uid": user_id},
        )
        await session.flush()

        try:
            self._storage.delete(key)
        except StorageError:
            logger.warning("S3 delete failed for key %r after DB row removed", key)


media_service = MediaService(storage_service)
