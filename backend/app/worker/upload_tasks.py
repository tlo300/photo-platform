"""Celery task for processing direct file uploads (issue #91).

Processing pipeline per uploaded file:
  1. Download staged file from S3
  2. SHA-256 checksum → skip if already owned by this user (dedup)
  3. Magic-byte MIME validation
  4. Upload original to MinIO under {user_id}/{asset_id}/original.ext
  5. Write MediaAsset row + increment users.storage_used_bytes
  6. Extract EXIF → set asset.captured_at
  7. apply_exif → write MediaMetadata row
  8. Album linking:
     - If rel_path has directory components → ensure album hierarchy (rooted at
       target_album_id when provided)
     - Else if target_album_id is set → link asset directly to that album
  9. Dispatch thumbnail generation
 10. Delete staging key from S3

Failed files are recorded in import_jobs.errors and never abort the job.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import logging
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath

import filetype as _filetype
from botocore.exceptions import ClientError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.album import Album, AlbumAsset
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.media import MediaAsset
from app.services.exif import apply_exif, extract_exif
from app.services.storage import StorageError, storage_service
from app.services.upload_validation import ALLOWED_MIME_TYPES
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)

_SUFFIX_MAP: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/x-matroska": ".mkv",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _detect_mime(data: bytes) -> str | None:
    kind = _filetype.guess(data[:512])
    if kind is None:
        return None
    return kind.mime if kind.mime in ALLOWED_MIME_TYPES else None


# ---------------------------------------------------------------------------
# Album helpers (local copies — avoids circular imports with takeout_tasks)
# ---------------------------------------------------------------------------


async def _get_or_create_album(
    session: AsyncSession,
    owner_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    title: str,
) -> uuid.UUID:
    """Return the id of an existing album matching (owner, parent, title), creating if absent."""
    if parent_id is None:
        stmt = select(Album.id).where(
            Album.owner_id == owner_id,
            Album.title == title,
            Album.parent_id.is_(None),
        )
    else:
        stmt = select(Album.id).where(
            Album.owner_id == owner_id,
            Album.title == title,
            Album.parent_id == parent_id,
        )
    existing = await session.scalar(stmt)
    if existing is not None:
        return existing

    album_id = uuid.uuid4()
    session.add(Album(id=album_id, owner_id=owner_id, parent_id=parent_id, title=title))
    await session.flush()
    return album_id


async def _ensure_album_path(
    session: AsyncSession,
    owner_id: uuid.UUID,
    folder_path: str,
    root_album_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Walk folder_path segments and return the deepest album id, creating albums as needed.

    When *root_album_id* is provided the first path segment is created under that
    album rather than at the top level.

    Examples:
        "vacation/beach"  → creates/reuses "vacation" then "beach", returns beach's id
        "vacation/beach" with root_album_id=X  → creates under X
    """
    parts = [p for p in PurePosixPath(folder_path).parts if p and p != "."]
    if not parts:
        return root_album_id

    parent_id: uuid.UUID | None = root_album_id
    for part in parts:
        parent_id = await _get_or_create_album(session, owner_id, parent_id, part)
    return parent_id


async def _link_asset_to_album(
    session: AsyncSession,
    album_id: uuid.UUID,
    asset_id: uuid.UUID,
) -> None:
    """Insert an album_assets row, ignoring duplicates."""
    await session.execute(
        text(
            "INSERT INTO album_assets (album_id, asset_id, sort_order)"
            " VALUES (:album_id, :asset_id, 0)"
            " ON CONFLICT DO NOTHING"
        ),
        {"album_id": album_id, "asset_id": asset_id},
    )


# ---------------------------------------------------------------------------
# Per-file ingestion
# ---------------------------------------------------------------------------


async def _ingest_one(
    session: AsyncSession,
    job: ImportJob,
    owner_id: uuid.UUID,
    data: bytes,
    filename: str,
    rel_path: str,
    target_album_id: uuid.UUID | None,
) -> None:
    """Ingest a single media file from raw bytes.  Errors are caught and appended to job.errors."""
    checksum = _sha256(data)

    # Dedup check — outside the savepoint
    existing = await session.scalar(
        select(MediaAsset.id).where(
            MediaAsset.owner_id == owner_id,
            MediaAsset.checksum == checksum,
        )
    )
    if existing is not None:
        logger.debug("Skipping duplicate %s (checksum %s)", filename, checksum)
        job.duplicates += 1
        dir_part = str(PurePosixPath(rel_path).parent) if rel_path else ""
        has_dir = dir_part and dir_part != "."
        if has_dir:
            album_id = await _ensure_album_path(
                session, owner_id, dir_part, root_album_id=target_album_id
            )
            if album_id is not None:
                await _link_asset_to_album(session, album_id, existing)
        elif target_album_id is not None:
            await _link_asset_to_album(session, target_album_id, existing)
        return

    staged_key: str | None = None
    try:
        async with session.begin_nested():
            mime = _detect_mime(data)
            if mime is None:
                raise ValueError("Unsupported or undetectable file type")

            asset_id = uuid.uuid4()
            suffix = _SUFFIX_MAP.get(mime, "")

            staged_key = storage_service.upload(
                str(owner_id),
                str(asset_id),
                io.BytesIO(data),
                suffix,
                mime,
            )

            asset = MediaAsset(
                id=asset_id,
                owner_id=owner_id,
                file_size_bytes=len(data),
                original_filename=filename,
                mime_type=mime,
                storage_key=staged_key,
                checksum=checksum,
            )
            session.add(asset)
            await session.execute(
                text(
                    "UPDATE users"
                    " SET storage_used_bytes = storage_used_bytes + :delta"
                    " WHERE id = :uid"
                ),
                {"delta": len(data), "uid": owner_id},
            )
            await session.flush()

            # EXIF — no sidecar for direct uploads; fall back to now() if no EXIF date
            exif_result = extract_exif(data, mime)
            if exif_result.captured_at is not None:
                asset.captured_at = exif_result.captured_at
            else:
                asset.captured_at = datetime.now(timezone.utc)
                asset.sidecar_missing = True
                job.no_sidecar += 1

            session.add(asset)
            await apply_exif(session, asset_id=asset_id, result=exif_result)

            # Album linking
            dir_part = str(PurePosixPath(rel_path).parent) if rel_path else ""
            has_dir = dir_part and dir_part != "."

            if has_dir:
                # Folder upload — recreate hierarchy, optionally rooted at target_album_id
                album_id = await _ensure_album_path(
                    session, owner_id, dir_part, root_album_id=target_album_id
                )
                if album_id is not None:
                    await _link_asset_to_album(session, album_id, asset_id)
            elif target_album_id is not None:
                # Flat upload with explicit album
                await _link_asset_to_album(session, target_album_id, asset_id)

            await session.flush()

        # Dispatch thumbnail generation outside the savepoint
        from app.worker.thumbnail_tasks import generate_thumbnails
        generate_thumbnails.delay(str(asset_id), str(owner_id))

    except Exception as exc:
        logger.warning("Failed to ingest %s: %s", filename, exc)
        if staged_key is not None:
            with contextlib.suppress(StorageError):
                storage_service.delete(staged_key)
        errors = list(job.errors or [])
        errors.append({"filename": filename, "reason": str(exc)})
        job.errors = errors


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------


async def _run_direct_upload(job_id: uuid.UUID, owner_id: uuid.UUID) -> None:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with factory() as session:
            await session.execute(
                text(f"SET LOCAL app.current_user_id = '{owner_id}'")
            )
            job = await session.get(ImportJob, job_id)
            if job is None:
                logger.error("ImportJob %s not found — aborting", job_id)
                return

            upload_keys: list[dict] = list(job.upload_keys or [])
            target_album_id: uuid.UUID | None = job.target_album_id

            job.total = len(upload_keys)
            job.status = ImportJobStatus.processing
            await session.commit()
            await session.execute(
                text(f"SET LOCAL app.current_user_id = '{owner_id}'")
            )

            for entry in upload_keys:
                key: str = entry["key"]
                filename: str = entry.get("filename", "upload")
                rel_path: str = entry.get("rel_path", "")

                # Download staged file from S3
                try:
                    buf = io.BytesIO()
                    storage_service._client.download_fileobj(
                        storage_service._bucket, key, buf
                    )
                    data = buf.getvalue()
                except ClientError as exc:
                    logger.warning("Could not download staged file %s: %s", key, exc)
                    errors = list(job.errors or [])
                    errors.append({"filename": filename, "reason": f"Staging download failed: {exc}"})
                    job.errors = errors
                    job.processed += 1
                    await session.commit()
                    await session.execute(
                        text(f"SET LOCAL app.current_user_id = '{owner_id}'")
                    )
                    continue

                await _ingest_one(
                    session, job, owner_id, data, filename, rel_path, target_album_id
                )
                job.processed += 1
                await session.commit()
                await session.execute(
                    text(f"SET LOCAL app.current_user_id = '{owner_id}'")
                )

                # Delete staging key regardless of ingest outcome
                with contextlib.suppress(StorageError):
                    storage_service.delete(key)

            job.status = ImportJobStatus.done
            await session.commit()

    except Exception as exc:
        logger.exception("Fatal error processing direct upload job %s", job_id)
        # Best-effort status update
        try:
            async with factory() as session:
                await session.execute(
                    text(f"SET LOCAL app.current_user_id = '{owner_id}'")
                )
                job = await session.get(ImportJob, job_id)
                if job is not None:
                    job.status = ImportJobStatus.failed
                    errors = list(job.errors or [])
                    errors.append({"filename": None, "reason": str(exc)})
                    job.errors = errors
                    await session.commit()
        except Exception:
            logger.exception("Could not update job %s status to failed", job_id)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------


@celery_app.task(name="upload.process_direct", bind=True, max_retries=0)
def process_direct_upload(self, job_id: str, owner_id: str) -> None:
    """Process all staged files for a direct upload job."""
    asyncio.run(_run_direct_upload(uuid.UUID(job_id), uuid.UUID(owner_id)))
