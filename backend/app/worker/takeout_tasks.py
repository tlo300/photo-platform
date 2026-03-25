"""Celery task for processing Google Takeout zip imports.

The task is intentionally synchronous (standard Celery pool) and calls the
existing async service layer via asyncio.run() so no separate sync DB engine
is required.

Processing pipeline per media file:
  1. SHA-256 checksum → skip if already owned by this user (dedup)
  2. Magic-byte validation against the allowed MIME whitelist
  3. Upload original to MinIO under {user_id}/{asset_id}/original.ext
  4. Write MediaAsset row + increment users.storage_used_bytes
  5. Extract EXIF metadata → write MediaMetadata row
  6. Parse sidecar (if present) → write Location, tags, raw JSON
     Sidecar timestamp overrides EXIF timestamp per architecture rule.

Failed files are recorded in import_jobs.errors and never abort the job.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import tempfile
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.media import MediaAsset
from app.services.exif import apply_exif, extract_exif
from app.services.storage import storage_service
from app.services.takeout_sidecar import apply_sidecar, parse_sidecar
from app.services.upload_validation import ALLOWED_MIME_TYPES, check_zip_safe
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)

# Sidecar extension used by Google Takeout
_SIDECAR_EXT = ".json"

# Google Takeout truncates filenames to 46 characters (before the extension)
# when creating sidecar names for files with long names.
_TAKEOUT_TRUNCATE = 46


def _sidecar_name(media_name: str) -> str:
    """Return the expected Takeout sidecar name for *media_name*.

    Google Takeout appends '.json' to the full filename.  For filenames
    whose base (without extension) exceeds ``_TAKEOUT_TRUNCATE`` characters
    the base is truncated before adding '.json'.

    Examples:
        photo.jpg          → photo.jpg.json
        very_long…name.jpg → very_long…name(46 chars).jpg.json  (truncated)
    """
    p = PurePosixPath(media_name)
    stem = p.stem
    suffix = p.suffix  # e.g. '.jpg'

    if len(stem) > _TAKEOUT_TRUNCATE:
        truncated_stem = stem[:_TAKEOUT_TRUNCATE]
        return truncated_stem + suffix + _SIDECAR_EXT

    return media_name + _SIDECAR_EXT


def _is_media_entry(name: str) -> bool:
    """Return True if *name* looks like a media file (not a sidecar or dir)."""
    lower = name.lower()
    if lower.endswith(_SIDECAR_EXT):
        return False
    # Skip macOS metadata entries and directory entries
    if "__macosx/" in lower or lower.endswith("/"):
        return False
    return True


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mime_from_magic(data: bytes) -> str | None:
    """Return detected MIME type string or None if undetectable / not whitelisted."""
    import filetype as _filetype

    kind = _filetype.guess(data[:512])
    if kind is None:
        return None
    return kind.mime if kind.mime in ALLOWED_MIME_TYPES else None


def _suffix_for_mime(mime: str) -> str:
    _MAP = {
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
    return _MAP.get(mime, "")


# ---------------------------------------------------------------------------
# Async core — called from the sync Celery task via asyncio.run()
# ---------------------------------------------------------------------------


async def _run_import(job_id: uuid.UUID, owner_id: uuid.UUID, zip_path: str) -> None:
    """Process *zip_path* and update the ImportJob row as files are ingested."""
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with factory() as session:
            await _set_rls(session, owner_id)
            await _process(session, job_id, owner_id, zip_path)
    finally:
        await engine.dispose()


async def _set_rls(session: AsyncSession, owner_id: uuid.UUID) -> None:
    await session.execute(
        text(f"SET LOCAL app.current_user_id = '{owner_id}'")
    )


async def _process(
    session: AsyncSession,
    job_id: uuid.UUID,
    owner_id: uuid.UUID,
    zip_path: str,
) -> None:
    job = await session.get(ImportJob, job_id)
    if job is None:
        logger.error("ImportJob %s not found — aborting", job_id)
        return

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp = Path(tmp_dir)
                check_zip_safe(zf, tmp)

                # Collect media entries and build sidecar lookup by lowercase path
                all_names = zf.namelist()
                media_names = [n for n in all_names if _is_media_entry(n)]
                sidecar_set = {n.lower() for n in all_names if n.lower().endswith(_SIDECAR_EXT)}

                job.total = len(media_names)
                job.status = ImportJobStatus.processing
                await session.commit()
                await _set_rls(session, owner_id)

                for media_name in media_names:
                    await _ingest_one(
                        session, job, owner_id, zf, media_name, sidecar_set
                    )
                    job.processed += 1
                    await session.commit()
                    await _set_rls(session, owner_id)

        job.status = ImportJobStatus.done
        await session.commit()

    except Exception as exc:
        logger.exception("Fatal error processing import job %s", job_id)
        job.status = ImportJobStatus.failed
        errors = list(job.errors or [])
        errors.append({"filename": None, "reason": str(exc)})
        job.errors = errors
        await session.commit()


async def _ingest_one(
    session: AsyncSession,
    job: ImportJob,
    owner_id: uuid.UUID,
    zf: zipfile.ZipFile,
    media_name: str,
    sidecar_set: set[str],
) -> None:
    """Ingest a single media file from the zip.  Errors are caught and appended to job.errors."""
    try:
        data = zf.read(media_name)
        checksum = _sha256_bytes(data)

        # Dedup check — skip silently if the file already exists for this user
        existing = await session.scalar(
            select(MediaAsset.id).where(
                MediaAsset.owner_id == owner_id,
                MediaAsset.checksum == checksum,
            )
        )
        if existing is not None:
            logger.debug("Skipping duplicate %s (checksum %s)", media_name, checksum)
            return

        mime = _mime_from_magic(data)
        if mime is None:
            raise ValueError(f"Unsupported or undetectable file type")

        asset_id = uuid.uuid4()
        suffix = _suffix_for_mime(mime)
        original_filename = Path(media_name).name

        key = storage_service.upload(
            str(owner_id),
            str(asset_id),
            io.BytesIO(data),
            suffix,
            mime,
        )

        try:
            from app.models.media import MediaAsset as _MA

            asset = _MA(
                id=asset_id,
                owner_id=owner_id,
                file_size_bytes=len(data),
                original_filename=original_filename,
                mime_type=mime,
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
                {"delta": len(data), "uid": owner_id},
            )
            await session.flush()
        except Exception:
            import contextlib
            from app.services.storage import StorageError
            with contextlib.suppress(StorageError):
                storage_service.delete(key)
            raise

        # EXIF extraction
        exif_result = extract_exif(data, mime)

        # Sidecar lookup — try standard name then truncated variant
        sidecar_data: dict | None = None
        for candidate in (_sidecar_name(media_name), _sidecar_name(Path(media_name).name)):
            if candidate.lower() in sidecar_set:
                try:
                    raw_bytes = zf.read(candidate) if candidate in zf.namelist() else None
                    if raw_bytes is None:
                        # Try case-insensitive match
                        for real_name in zf.namelist():
                            if real_name.lower() == candidate.lower():
                                raw_bytes = zf.read(real_name)
                                break
                    if raw_bytes:
                        sidecar_data = json.loads(raw_bytes.decode("utf-8", errors="replace"))
                except Exception as exc:
                    logger.warning("Could not read sidecar %s: %s", candidate, exc)
                break

        parsed_sidecar = parse_sidecar(sidecar_data) if sidecar_data else None

        await apply_exif(
            session,
            asset_id=asset_id,
            result=exif_result,
            sidecar_captured_at=parsed_sidecar.captured_at if parsed_sidecar else None,
        )

        if parsed_sidecar is not None:
            await apply_sidecar(
                session,
                asset_id=asset_id,
                owner_id=owner_id,
                parsed=parsed_sidecar,
            )

        await session.flush()

    except Exception as exc:
        logger.warning("Failed to ingest %s: %s", media_name, exc)
        errors = list(job.errors or [])
        errors.append({"filename": Path(media_name).name, "reason": str(exc)})
        job.errors = errors
        await session.rollback()
        await _set_rls(session, owner_id)


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------


@celery_app.task(name="takeout.process_zip", bind=True, max_retries=0)
def process_takeout_zip(self, job_id: str, owner_id: str) -> None:
    """Download the staged zip from S3, process every media file, update job progress.

    The zip is downloaded to a temporary file, processed, and the temp file is
    removed when done.  The staged S3 zip key is deleted after successful processing.
    """
    job_uuid = uuid.UUID(job_id)
    owner_uuid = uuid.UUID(owner_id)

    zip_tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    zip_path = zip_tmp.name
    zip_tmp.close()

    try:
        # Determine zip key from the job row (need a quick sync read or just infer)
        # We reconstruct the key using the convention set by the API endpoint.
        zip_key = asyncio.run(_get_zip_key(job_uuid, owner_uuid))
        if zip_key is None:
            logger.error("ImportJob %s not found; aborting", job_id)
            return

        # Download zip from S3 to temp file
        storage_service._client.download_file(
            storage_service._bucket,
            zip_key,
            zip_path,
        )

        asyncio.run(_run_import(job_uuid, owner_uuid, zip_path))

        # Clean up the staged zip from S3 after successful (or failed-but-recorded) processing
        try:
            storage_service.delete(zip_key)
        except Exception:
            logger.warning("Could not delete staged zip %s from S3", zip_key)

    finally:
        try:
            os.unlink(zip_path)
        except OSError:
            pass


async def _get_zip_key(job_id: uuid.UUID, owner_id: uuid.UUID) -> str | None:
    """Return the zip_key for the given job, or None if not found."""
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await session.execute(
                text(f"SET LOCAL app.current_user_id = '{owner_id}'")
            )
            job = await session.get(ImportJob, job_id)
            return job.zip_key if job else None
    finally:
        await engine.dispose()
