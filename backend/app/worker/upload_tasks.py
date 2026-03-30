"""Celery task for processing direct file uploads (issue #91).

Processing pipeline per uploaded file:
  1. Download staged file from S3
  2. SHA-256 checksum → skip if already owned by this user (dedup)
  3. Magic-byte MIME validation
  4. Upload original to MinIO under {user_id}/{asset_id}/original.ext
  5. Write MediaAsset row + increment users.storage_used_bytes
  6. Extract EXIF → set asset.captured_at
  7. apply_exif → write MediaMetadata row
  8. Insert Location row from EXIF GPS + dispatch geocode task
  9. Album linking:
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
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath

import filetype as _filetype
from botocore.exceptions import ClientError
from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.album import Album, AlbumAsset
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.media import Location, MediaAsset
from app.services.exif import apply_exif, extract_exif
from app.services.metadata_merge import merge_metadata
from app.services.storage import StorageError, storage_service
from app.services.takeout_sidecar import ParsedSidecar, parse_sidecar
from app.services.upload_validation import ALLOWED_MIME_TYPES
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)

# Matches "Photos from 2003" (and variants) anywhere in a file path.
_PHOTOS_FROM_YEAR_RE = re.compile(r"\bphotos?\s+from\s+(\d{4})\b", re.IGNORECASE)


def _folder_year(path: str) -> int | None:
    """Return the year embedded in a 'Photos from YYYY' path component, or None."""
    m = _PHOTOS_FROM_YEAR_RE.search(path)
    if m:
        year = int(m.group(1))
        if 1900 <= year <= 2100:
            return year
    return None

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

# Extension fallback for video formats whose ISO-BMFF ftyp brands are not
# fully covered by the filetype library (e.g. iPhone HEVC videos with M4V /
# hvc1 brands, GoPro files with non-standard brands).
_EXT_FALLBACK: dict[str, str] = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
}

# Extensions used to identify Live Photo stills and companion videos.
_LIVE_STILL_EXTS = frozenset({".heic", ".heif", ".jpg", ".jpeg"})
_LIVE_VIDEO_EXTS = frozenset({".mp4", ".mov"})


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _detect_mime(data: bytes, filename: str = "") -> str | None:
    kind = _filetype.guess(data[:512])
    if kind is not None and kind.mime in ALLOWED_MIME_TYPES:
        return kind.mime
    # Fallback: trust file extension for video formats whose container
    # variants are not fully covered by the filetype library.
    if filename and "." in filename:
        ext = "." + filename.rsplit(".", 1)[-1].lower()
        return _EXT_FALLBACK.get(ext)
    return None


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
    parsed_sidecar: ParsedSidecar | None = None,
    live_video_data: bytes | None = None,
    live_video_filename: str | None = None,
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
        # If a sidecar provides a valid date, update the existing asset's captured_at.
        # This fixes photos previously imported without sidecar data (wrong EXIF clock).
        if parsed_sidecar is not None:
            canonical = merge_metadata(None, parsed_sidecar)
            if canonical.captured_at is not None:
                existing_asset = await session.get(MediaAsset, existing)
                if existing_asset is not None:
                    existing_asset.captured_at = canonical.captured_at
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
    staged_live_key: str | None = None
    try:
        async with session.begin_nested():
            mime = _detect_mime(data, filename)
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

            # Live Photo companion upload — triggered when a paired video was provided.
            if live_video_data is not None and live_video_filename is not None:
                _live_ext = PurePosixPath(live_video_filename).suffix.lower() or ".mp4"
                _live_mime = "video/mp4" if _live_ext == ".mp4" else "video/quicktime"
                staged_live_key = storage_service.upload_live_video(
                    str(owner_id), str(asset_id), io.BytesIO(live_video_data),
                    _live_ext, _live_mime,
                )
                logger.info(
                    "Live Photo pair: still=%s video=%s", filename, live_video_filename
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
            if staged_live_key is not None:
                asset.is_live_photo = True
                asset.live_video_key = staged_live_key
            session.add(asset)
            await session.execute(
                text(
                    "UPDATE users"
                    " SET storage_used_bytes = storage_used_bytes + :delta"
                    " WHERE id = :uid"
                ),
                {"delta": len(data) + (len(live_video_data) if live_video_data else 0), "uid": owner_id},
            )
            await session.flush()

            # Resolve captured_at via the canonical merge strategy (same as Takeout path).
            # parsed_sidecar is populated when a matching .json sidecar was uploaded
            # alongside this file (e.g. a Google Takeout folder structure).
            exif_result = extract_exif(data, mime)
            canonical = merge_metadata(exif_result, parsed_sidecar)
            if canonical.captured_at is not None:
                captured = canonical.captured_at
                # Correct a wrong year using the "Photos from YYYY" folder name
                # (Google Takeout convention). Applied regardless of sidecar presence
                # because Google controls these folder names and they are reliable.
                year = _folder_year(rel_path)
                if year and year != captured.year:
                    try:
                        captured = captured.replace(year=year)
                    except ValueError:
                        pass  # e.g. Feb 29 in a non-leap year — keep original
                asset.captured_at = captured
            else:
                asset.captured_at = datetime.now(timezone.utc)
                asset.sidecar_missing = True
                job.no_sidecar += 1

            session.add(asset)
            await apply_exif(session, asset_id=asset_id, result=exif_result)

            # Insert location from EXIF GPS (same pattern as metadata_tasks._apply_metadata)
            new_location = False
            if exif_result.gps_latitude is not None and exif_result.gps_longitude is not None:
                from geoalchemy2.functions import ST_MakePoint
                stmt = (
                    pg_insert(Location)
                    .values(
                        asset_id=asset_id,
                        point=ST_MakePoint(exif_result.gps_longitude, exif_result.gps_latitude),
                        altitude_metres=exif_result.gps_altitude,
                    )
                    .on_conflict_do_nothing(index_elements=["asset_id"])
                )
                await session.execute(stmt)
                new_location = True

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

        if new_location and exif_result.gps_latitude is not None:
            from app.worker.geocode_tasks import resolve_asset_geocode
            resolve_asset_geocode.delay(
                str(asset_id), str(owner_id),
                exif_result.gps_latitude, exif_result.gps_longitude,
            )

    except Exception as exc:
        logger.warning("Failed to ingest %s: %s", filename, exc)
        if staged_key is not None:
            with contextlib.suppress(StorageError):
                storage_service.delete(staged_key)
        if staged_live_key is not None:
            with contextlib.suppress(StorageError):
                storage_service.delete(staged_live_key)
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

            all_keys: list[dict] = list(job.upload_keys or [])
            target_album_id: uuid.UUID | None = job.target_album_id

            # Separate sidecar JSON entries from media entries.
            # Sidecars are named "<media_filename>.json" (Google Takeout convention).
            sidecar_entries = [e for e in all_keys if e.get("filename", "").lower().endswith(".json")]
            media_entries = [e for e in all_keys if not e.get("filename", "").lower().endswith(".json")]

            # Download and parse sidecars, build lookup: media_filename → ParsedSidecar.
            sidecar_lookup: dict[str, ParsedSidecar] = {}
            for entry in sidecar_entries:
                key: str = entry["key"]
                filename: str = entry.get("filename", "")
                # Strip the sidecar suffix to get the media filename.
                # "photo.jpg.supplemental-metadata.json" → "photo.jpg"
                # "photo.jpg.json" → "photo.jpg"
                if filename.lower().endswith(".supplemental-metadata.json"):
                    media_filename = filename[: -len(".supplemental-metadata.json")]
                elif filename.lower().endswith(".json"):
                    media_filename = filename[:-5]
                else:
                    media_filename = filename
                try:
                    buf = io.BytesIO()
                    storage_service._client.download_fileobj(
                        storage_service._bucket, key, buf
                    )
                    raw_json = json.loads(buf.getvalue().decode("utf-8", errors="replace"))
                    # Lowercase key for case-insensitive matching (PICT0049.JPG → pict0049.jpg)
                    sidecar_lookup[media_filename.lower()] = parse_sidecar(raw_json)
                except Exception as exc:
                    logger.warning("Could not process sidecar %s: %s", filename, exc)
                finally:
                    with contextlib.suppress(StorageError):
                        storage_service.delete(key)

            # --- Live Photo pair detection ---
            # Match stills (HEIC/JPG) with companion videos (MP4/MOV) by
            # (parent_dir_lower, stem_lower) so a folder of live photos gets paired.
            _pair_candidates: dict[tuple[str, str], dict[str, dict]] = {}
            for _e in media_entries:
                _fn = _e.get("filename", "")
                _p = PurePosixPath(_fn)
                _ext = _p.suffix.lower()
                _pkey = (str(_p.parent).lower(), _p.stem.lower())
                if _ext in _LIVE_STILL_EXTS:
                    _pair_candidates.setdefault(_pkey, {})["still"] = _e
                elif _ext in _LIVE_VIDEO_EXTS:
                    _pair_candidates.setdefault(_pkey, {})["video"] = _e
            live_photo_pairs: dict[tuple[str, str], dict[str, dict]] = {
                k: v for k, v in _pair_candidates.items()
                if "still" in v and "video" in v
            }
            # Staging keys of companion videos — skip them as standalone entries
            paired_video_staging_keys: set[str] = {
                sides["video"]["key"] for sides in live_photo_pairs.values()
            }

            job.total = len(media_entries) - len(paired_video_staging_keys)
            job.status = ImportJobStatus.processing
            await session.commit()
            await session.execute(
                text(f"SET LOCAL app.current_user_id = '{owner_id}'")
            )

            for entry in media_entries:
                key = entry["key"]

                # Skip companion videos — they are ingested as part of their paired still.
                if key in paired_video_staging_keys:
                    continue

                filename = entry.get("filename", "upload")
                rel_path: str = entry.get("rel_path", "")

                parsed_sidecar = sidecar_lookup.get(filename.lower())
                # Live photo companion fallback: MP4/MOV shares stem with HEIC/HEIF sidecar.
                if parsed_sidecar is None:
                    _stem = PurePosixPath(filename).stem.lower()
                    for _photo_ext in (".heic", ".heif", ".jpg", ".jpeg"):
                        parsed_sidecar = sidecar_lookup.get(_stem + _photo_ext)
                        if parsed_sidecar is not None:
                            break

                # Check whether this still has a paired companion video.
                _fp = PurePosixPath(filename)
                _pair_key = (str(_fp.parent).lower(), _fp.stem.lower())
                _pair_sides = live_photo_pairs.get(_pair_key)

                live_video_data: bytes | None = None
                live_video_filename: str | None = None
                live_video_staging_key: str | None = None

                if _pair_sides is not None:
                    _video_entry = _pair_sides["video"]
                    live_video_staging_key = _video_entry["key"]
                    _vfn = _video_entry.get("filename", "live.mp4")
                    try:
                        _vbuf = io.BytesIO()
                        storage_service._client.download_fileobj(
                            storage_service._bucket, live_video_staging_key, _vbuf
                        )
                        live_video_data = _vbuf.getvalue()
                        live_video_filename = _vfn
                    except ClientError as exc:
                        logger.warning(
                            "Could not download companion video %s: %s", _vfn, exc
                        )
                        # Proceed without live pairing; still clean up the staging key below.

                # Download the still's staged file from S3.
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
                    # Clean up companion staging key even when the still fails to download.
                    if live_video_staging_key is not None:
                        with contextlib.suppress(StorageError):
                            storage_service.delete(live_video_staging_key)
                    continue

                await _ingest_one(
                    session, job, owner_id, data, filename, rel_path, target_album_id,
                    parsed_sidecar=parsed_sidecar,
                    live_video_data=live_video_data,
                    live_video_filename=live_video_filename,
                )
                job.processed += 1
                await session.commit()
                await session.execute(
                    text(f"SET LOCAL app.current_user_id = '{owner_id}'")
                )

                # Delete staging keys regardless of ingest outcome.
                with contextlib.suppress(StorageError):
                    storage_service.delete(key)
                if live_video_staging_key is not None:
                    with contextlib.suppress(StorageError):
                        storage_service.delete(live_video_staging_key)

            # Retroactive date fix: for sidecars whose photo was not re-uploaded
            # (dedup-filtered on the client), update captured_at of existing assets.
            if sidecar_lookup:
                processed_lower = {e.get("filename", "").lower() for e in media_entries}
                for media_filename_lower, sidecar in sidecar_lookup.items():
                    if media_filename_lower in processed_lower:
                        continue  # already handled in _ingest_one
                    if not sidecar.captured_at:
                        continue
                    canonical = merge_metadata(None, sidecar)
                    if not canonical.captured_at:
                        continue
                    # Match by full path OR by basename — the browser sends only the
                    # basename via file.name, but original_filename may be stored with
                    # a folder prefix (e.g. "Folder/PICT0049.JPG").
                    basename_lower = PurePosixPath(media_filename_lower).name
                    result = await session.execute(
                        select(MediaAsset).where(
                            MediaAsset.owner_id == owner_id,
                            or_(
                                func.lower(MediaAsset.original_filename) == media_filename_lower,
                                func.lower(MediaAsset.original_filename) == basename_lower,
                                func.lower(MediaAsset.original_filename).like(f"%/{basename_lower}"),
                            ),
                        ).limit(1)
                    )
                    existing_asset = result.scalar_one_or_none()
                    if existing_asset is not None:
                        existing_asset.captured_at = canonical.captured_at
                        logger.info("Updated captured_at for %s from sidecar", media_filename_lower)
                await session.commit()
                await session.execute(text(f"SET LOCAL app.current_user_id = '{owner_id}'"))

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
