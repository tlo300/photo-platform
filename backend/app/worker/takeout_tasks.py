"""Celery task for processing Google Takeout zip imports.

The task is intentionally synchronous (standard Celery pool) and calls the
existing async service layer via asyncio.run() so no separate sync DB engine
is required.

Processing pipeline per media file:
  1. SHA-256 checksum → skip if already owned by this user (dedup)
  2. Magic-byte validation against the allowed MIME whitelist
  3. Upload original to MinIO under {user_id}/{asset_id}/original.ext
  4. Write MediaAsset row + increment users.storage_used_bytes
  5. Extract EXIF → merge_metadata(exif, sidecar) → set asset.captured_at
  6. apply_exif → write MediaMetadata row (make/model/dimensions only)
  7. apply_sidecar (if present) → write Location, tags, raw JSON

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
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.album import Album, AlbumAsset
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.media import MediaAsset
from app.services.exif import apply_exif, extract_exif
from app.services.metadata_merge import merge_metadata
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


def _mtime_from_zip_info(info: zipfile.ZipInfo) -> datetime:
    """Return the last-modified timestamp from a ZipInfo entry as a UTC datetime.

    ZipInfo.date_time is a 6-tuple (year, month, day, hour, min, sec).
    Zip timestamps have no timezone — we treat them as UTC, which is a safe
    last-resort fallback consistent with what most tools assume.
    """
    year, month, day, hour, minute, second = info.date_time
    # ZipInfo can store seconds as 0 for entries with no time; clamp to valid range.
    return datetime(
        max(year, 1980), max(month, 1), max(day, 1),
        hour, minute, min(second, 59),
        tzinfo=timezone.utc,
    )


# ---------------------------------------------------------------------------
# Album hierarchy helpers
# ---------------------------------------------------------------------------


def _zip_folder_path(media_name: str) -> str | None:
    """Return the folder portion of a zip entry path, or None for root-level files.

    Examples:
        "2001/january/photo.jpg" → "2001/january"
        "photo.jpg"              → None
    """
    parent = str(PurePosixPath(media_name).parent)
    return None if parent == "." else parent


async def _get_or_create_album(
    session: AsyncSession,
    owner_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    title: str,
) -> uuid.UUID:
    """Return the id of an existing album matching (owner, parent, title), creating it if absent."""
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
) -> uuid.UUID | None:
    """Walk folder_path segments and return the deepest album id, creating albums as needed.

    Examples:
        "2001"          → creates/reuses album "2001" (root), returns its id
        "2001/january"  → creates/reuses "2001" then "january" (child), returns january's id
    """
    parts = [p for p in PurePosixPath(folder_path).parts if p and p != "."]
    if not parts:
        return None

    parent_id: uuid.UUID | None = None
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
            "INSERT INTO album_assets (album_id, asset_id)"
            " VALUES (:album_id, :asset_id)"
            " ON CONFLICT DO NOTHING"
        ),
        {"album_id": album_id, "asset_id": asset_id},
    )


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
    import contextlib
    from app.services.storage import StorageError

    data = zf.read(media_name)
    checksum = _sha256_bytes(data)

    # Dedup check — outside the savepoint so we don't need to roll it back
    existing = await session.scalar(
        select(MediaAsset.id).where(
            MediaAsset.owner_id == owner_id,
            MediaAsset.checksum == checksum,
        )
    )
    if existing is not None:
        logger.debug("Skipping duplicate %s (checksum %s)", media_name, checksum)
        job.duplicates += 1
        return

    staged_key: str | None = None
    try:
        async with session.begin_nested():
            # All DB writes are inside a savepoint — on failure the savepoint is
            # rolled back automatically without touching the outer transaction.
            mime = _mime_from_magic(data)
            if mime is None:
                raise ValueError("Unsupported or undetectable file type")

            asset_id = uuid.uuid4()
            suffix = _suffix_for_mime(mime)
            original_filename = Path(media_name).name

            staged_key = storage_service.upload(
                str(owner_id),
                str(asset_id),
                io.BytesIO(data),
                suffix,
                mime,
            )

            from app.models.media import MediaAsset as _MA

            asset = _MA(
                id=asset_id,
                owner_id=owner_id,
                file_size_bytes=len(data),
                original_filename=original_filename,
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

            # EXIF extraction
            exif_result = extract_exif(data, mime)

            # Sidecar lookup — try standard name then truncated variant
            sidecar_data: dict | None = None
            for candidate in (_sidecar_name(media_name), _sidecar_name(Path(media_name).name)):
                if candidate.lower() in sidecar_set:
                    try:
                        raw_bytes = zf.read(candidate) if candidate in zf.namelist() else None
                        if raw_bytes is None:
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
            sidecar_found = parsed_sidecar is not None or sidecar_data is not None

            if not sidecar_found:
                asset.sidecar_missing = True
                job.no_sidecar += 1

            # Resolve captured_at via the canonical merge strategy
            canonical = merge_metadata(exif_result, parsed_sidecar)
            if canonical.captured_at is not None:
                asset.captured_at = canonical.captured_at
            elif not sidecar_found:
                # Neither sidecar nor EXIF provided a date — fall back to the
                # zip entry's last-modified timestamp as a last resort.
                zip_info = zf.getinfo(media_name)
                asset.captured_at = _mtime_from_zip_info(zip_info)
                logger.warning(
                    "%s has no sidecar and no EXIF date — using zip mtime %s",
                    media_name,
                    asset.captured_at,
                )
            session.add(asset)

            await apply_exif(
                session,
                asset_id=asset_id,
                result=exif_result,
            )

            if parsed_sidecar is not None:
                await apply_sidecar(
                    session,
                    asset_id=asset_id,
                    owner_id=owner_id,
                    parsed=parsed_sidecar,
                )

            # Create/reuse album hierarchy from the zip entry's folder path
            folder_path = _zip_folder_path(media_name)
            if folder_path is not None:
                album_id = await _ensure_album_path(session, owner_id, folder_path)
                if album_id is not None:
                    await _link_asset_to_album(session, album_id, asset_id)

            await session.flush()

    except Exception as exc:
        logger.warning("Failed to ingest %s: %s", media_name, exc)
        # Clean up the staged S3 object if it was uploaded before the DB write failed
        if staged_key is not None:
            with contextlib.suppress(StorageError):
                storage_service.delete(staged_key)
        errors = list(job.errors or [])
        errors.append({"filename": Path(media_name).name, "reason": str(exc)})
        job.errors = errors
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


# ---------------------------------------------------------------------------
# Folder import — async core
# ---------------------------------------------------------------------------


def _is_media_path(p: Path) -> bool:
    """Return True if *p* looks like a media file (not a sidecar, not hidden)."""
    name = p.name
    if name.startswith("."):
        return False
    return _is_media_entry(name)


def _mtime_from_path(p: Path) -> datetime:
    """Return the file modification time as a UTC datetime."""
    return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)


async def _run_folder_import(
    job_id: uuid.UUID, owner_id: uuid.UUID, folder_path: str
) -> None:
    """Walk *folder_path* and ingest every media file, updating the ImportJob."""
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with factory() as session:
            await _set_rls(session, owner_id)
            await _process_folder(session, job_id, owner_id, Path(folder_path))
    finally:
        await engine.dispose()


async def _process_folder(
    session: AsyncSession,
    job_id: uuid.UUID,
    owner_id: uuid.UUID,
    folder: Path,
) -> None:
    job = await session.get(ImportJob, job_id)
    if job is None:
        logger.error("ImportJob %s not found — aborting", job_id)
        return

    try:
        media_paths = sorted(p for p in folder.rglob("*") if p.is_file() and _is_media_path(p))

        job.total = len(media_paths)
        job.status = ImportJobStatus.processing
        await session.commit()
        await _set_rls(session, owner_id)

        for media_path in media_paths:
            await _ingest_one_from_path(session, job, owner_id, media_path, folder)
            job.processed += 1
            await session.commit()
            await _set_rls(session, owner_id)

        job.status = ImportJobStatus.done
        await session.commit()

    except Exception as exc:
        logger.exception("Fatal error processing folder import job %s", job_id)
        job.status = ImportJobStatus.failed
        errors = list(job.errors or [])
        errors.append({"filename": None, "reason": str(exc)})
        job.errors = errors
        await session.commit()


async def _ingest_one_from_path(
    session: AsyncSession,
    job: ImportJob,
    owner_id: uuid.UUID,
    file_path: Path,
    import_root: Path,
) -> None:
    """Ingest a single media file from the filesystem. Errors are caught and appended to job.errors."""
    import contextlib
    from app.services.storage import StorageError

    data = file_path.read_bytes()
    checksum = _sha256_bytes(data)

    existing = await session.scalar(
        select(MediaAsset.id).where(
            MediaAsset.owner_id == owner_id,
            MediaAsset.checksum == checksum,
        )
    )
    if existing is not None:
        logger.debug("Skipping duplicate %s (checksum %s)", file_path.name, checksum)
        job.duplicates += 1
        return

    staged_key: str | None = None
    try:
        async with session.begin_nested():
            mime = _mime_from_magic(data)
            if mime is None:
                raise ValueError("Unsupported or undetectable file type")

            asset_id = uuid.uuid4()
            suffix = _suffix_for_mime(mime)

            staged_key = storage_service.upload(
                str(owner_id),
                str(asset_id),
                io.BytesIO(data),
                suffix,
                mime,
            )

            from app.models.media import MediaAsset as _MA

            asset = _MA(
                id=asset_id,
                owner_id=owner_id,
                file_size_bytes=len(data),
                original_filename=file_path.name,
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

            exif_result = extract_exif(data, mime)

            # Sidecar: look for {filename}.json adjacent to the media file
            sidecar_data: dict | None = None
            for candidate_name in (_sidecar_name(file_path.name), _sidecar_name(PurePosixPath(file_path.name).name)):
                candidate_path = file_path.parent / candidate_name
                if candidate_path.exists():
                    try:
                        sidecar_data = json.loads(candidate_path.read_text("utf-8", errors="replace"))
                    except Exception as exc:
                        logger.warning("Could not read sidecar %s: %s", candidate_path, exc)
                    break

            parsed_sidecar = parse_sidecar(sidecar_data) if sidecar_data else None
            sidecar_found = parsed_sidecar is not None or sidecar_data is not None

            if not sidecar_found:
                asset.sidecar_missing = True
                job.no_sidecar += 1

            canonical = merge_metadata(exif_result, parsed_sidecar)
            if canonical.captured_at is not None:
                asset.captured_at = canonical.captured_at
            elif not sidecar_found:
                asset.captured_at = _mtime_from_path(file_path)
                logger.warning(
                    "%s has no sidecar and no EXIF date — using file mtime %s",
                    file_path.name,
                    asset.captured_at,
                )
            session.add(asset)

            await apply_exif(session, asset_id=asset_id, result=exif_result)

            if parsed_sidecar is not None:
                await apply_sidecar(session, asset_id=asset_id, owner_id=owner_id, parsed=parsed_sidecar)

            # Create/reuse album hierarchy from the file's relative folder path
            try:
                rel_parent = str(PurePosixPath(file_path.relative_to(import_root).parent))
            except ValueError:
                rel_parent = None
            if rel_parent and rel_parent != ".":
                album_id = await _ensure_album_path(session, owner_id, rel_parent)
                if album_id is not None:
                    await _link_asset_to_album(session, album_id, asset_id)

            await session.flush()

    except Exception as exc:
        logger.warning("Failed to ingest %s: %s", file_path.name, exc)
        if staged_key is not None:
            with contextlib.suppress(StorageError):
                storage_service.delete(staged_key)
        errors = list(job.errors or [])
        errors.append({"filename": file_path.name, "reason": str(exc)})
        job.errors = errors
        await _set_rls(session, owner_id)


# ---------------------------------------------------------------------------
# Folder import — Celery task
# ---------------------------------------------------------------------------


@celery_app.task(name="takeout.process_folder", bind=True, max_retries=0)
def process_takeout_folder(self, job_id: str, owner_id: str) -> None:
    """Walk the local folder recorded in the ImportJob and ingest every media file."""
    job_uuid = uuid.UUID(job_id)
    owner_uuid = uuid.UUID(owner_id)

    folder_path = asyncio.run(_get_folder_path(job_uuid, owner_uuid))
    if folder_path is None:
        logger.error("ImportJob %s not found or has no folder_path; aborting", job_id)
        return

    asyncio.run(_run_folder_import(job_uuid, owner_uuid, folder_path))


async def _get_folder_path(job_id: uuid.UUID, owner_id: uuid.UUID) -> str | None:
    """Return the folder_path for the given job, or None if not found."""
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await session.execute(
                text(f"SET LOCAL app.current_user_id = '{owner_id}'")
            )
            job = await session.get(ImportJob, job_id)
            return job.folder_path if job else None
    finally:
        await engine.dispose()
