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
import re
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
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


# Sidecar extension used by Google Takeout
_SIDECAR_EXT = ".json"

# Extensions used to identify Live Photo stills (primary) and companion videos.
_LIVE_STILL_EXTS = frozenset({".heic", ".heif", ".jpg", ".jpeg"})
_LIVE_VIDEO_EXTS = frozenset({".mp4", ".mov"})
# Older / region-specific Takeout exports use this suffix instead of ".json".
_SUPPLEMENTAL_SIDECAR_EXT = ".supplemental-metadata.json"

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


@dataclass
class _AlbumMeta:
    """Title and description sourced from a Google Takeout album-level metadata.json."""

    title: str
    description: str | None = None


@dataclass
class _AlbumIndex:
    """Pre-scanned album metadata and per-file sort orders for a Takeout zip.

    meta        — folder_path → AlbumMeta (populated from folder-level metadata.json)
    sort_orders — zip_entry_name → sort_order within its containing folder
    """

    meta: dict[str, _AlbumMeta] = field(default_factory=dict)
    sort_orders: dict[str, int] = field(default_factory=dict)


def _build_album_index(zf: zipfile.ZipFile) -> _AlbumIndex:
    """Pre-scan *zf* to build album metadata and per-file sort orders.

    Two passes over the zip name list:
      1. Find folder-level ``metadata.json`` files → populate AlbumMeta by folder path.
      2. Read per-photo sidecars for ``photoTakenTime`` → sort media within each folder
         by (timestamp, filename) and assign sequential sort_order values.
    """
    all_names = zf.namelist()

    # Pass 1: album-level metadata.json files (name is exactly "metadata.json",
    # not a photo sidecar like "photo.jpg.json").
    folder_meta: dict[str, _AlbumMeta] = {}
    for name in all_names:
        p = PurePosixPath(name)
        if p.name != "metadata.json":
            continue
        folder = str(p.parent)
        if folder == ".":
            continue  # root-level metadata.json — not an album
        try:
            data = json.loads(zf.read(name).decode("utf-8", errors="replace"))
            raw_title = data.get("title") or ""
            title = raw_title.strip() or p.parent.name  # fall back to folder name
            raw_desc = data.get("description") or ""
            description = raw_desc.strip() or None
            folder_meta[folder] = _AlbumMeta(title=title, description=description)
        except Exception:
            pass

    # Pass 2: per-photo sidecars → timestamps.
    # Sidecar name pattern: "<media_filename>.json" (e.g., "photo.jpg.json").
    # folder → {media_basename → photoTakenTime timestamp (int)}
    folder_timestamps: dict[str, dict[str, int]] = {}
    for name in all_names:
        p = PurePosixPath(name)
        if not name.lower().endswith(_SIDECAR_EXT):
            continue
        if p.name == "metadata.json":
            continue  # album metadata, handled above
        folder = str(p.parent)
        try:
            data = json.loads(zf.read(name).decode("utf-8", errors="replace"))
            ts_raw = data.get("photoTakenTime", {}).get("timestamp", "0")
            ts = int(ts_raw)
            # Strip the sidecar suffix to get the media filename.
            # Handle both "photo.jpg.supplemental-metadata.json" and "photo.jpg.json".
            if name.lower().endswith(_SUPPLEMENTAL_SIDECAR_EXT):
                media_basename = PurePosixPath(name[: -len(_SUPPLEMENTAL_SIDECAR_EXT)]).name
            else:
                media_basename = PurePosixPath(name[: -len(_SIDECAR_EXT)]).name
            folder_timestamps.setdefault(folder, {})[media_basename] = ts
        except Exception:
            pass

    # Assign sort orders: group media files by folder, sort by (timestamp, name).
    folder_media: dict[str, list[str]] = {}
    for name in all_names:
        if _is_media_entry(name):
            p = PurePosixPath(name)
            folder = str(p.parent)
            folder_media.setdefault(folder, []).append(name)

    sort_orders: dict[str, int] = {}
    for folder, media_list in folder_media.items():
        timestamps = folder_timestamps.get(folder, {})

        def _sort_key(entry: str, _ts: dict[str, int] = timestamps) -> tuple[int, str]:
            return (_ts.get(PurePosixPath(entry).name, 0), entry)

        for order, entry in enumerate(sorted(media_list, key=_sort_key)):
            sort_orders[entry] = order

    return _AlbumIndex(meta=folder_meta, sort_orders=sort_orders)


async def _get_or_create_album(
    session: AsyncSession,
    owner_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    title: str,
    description: str | None = None,
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
    session.add(Album(id=album_id, owner_id=owner_id, parent_id=parent_id, title=title, description=description))
    await session.flush()
    return album_id


async def _ensure_album_path(
    session: AsyncSession,
    owner_id: uuid.UUID,
    folder_path: str,
    album_index: _AlbumIndex | None = None,
) -> uuid.UUID | None:
    """Walk folder_path segments and return the deepest album id, creating albums as needed.

    When *album_index* is provided and the leaf folder has a ``metadata.json`` entry in
    the index, the leaf album is created with the metadata title and description instead
    of the raw folder name.

    Examples:
        "2001"          → creates/reuses album "2001" (root), returns its id
        "2001/january"  → creates/reuses "2001" then "january" (child), returns january's id
    """
    parts = [p for p in PurePosixPath(folder_path).parts if p and p != "."]
    if not parts:
        return None

    parent_id: uuid.UUID | None = None
    for i, part in enumerate(parts):
        is_leaf = i == len(parts) - 1
        if is_leaf and album_index is not None:
            meta = album_index.meta.get(folder_path)
            title = meta.title if meta else part
            description = meta.description if meta else None
        else:
            title = part
            description = None
        parent_id = await _get_or_create_album(session, owner_id, parent_id, title, description)
    return parent_id


async def _link_asset_to_album(
    session: AsyncSession,
    album_id: uuid.UUID,
    asset_id: uuid.UUID,
    sort_order: int = 0,
) -> None:
    """Insert an album_assets row with the given sort_order, ignoring duplicates."""
    await session.execute(
        text(
            "INSERT INTO album_assets (album_id, asset_id, sort_order)"
            " VALUES (:album_id, :asset_id, :sort_order)"
            " ON CONFLICT DO NOTHING"
        ),
        {"album_id": album_id, "asset_id": asset_id, "sort_order": sort_order},
    )


# ---------------------------------------------------------------------------
# Live Photo pairing helpers
# ---------------------------------------------------------------------------


def _build_live_photo_pairs(
    all_names: list[str],
) -> dict[tuple[str, str], dict[str, zipfile.ZipInfo]]:
    """Pre-scan *all_names* and return a map of Live Photo still+video pairs.

    Returns a dict keyed by ``(dir_prefix, stem_lower)`` where each value is a
    sub-dict with one or both of ``"still"`` and ``"video"`` keys holding the
    raw zip entry name.  Only entries that have **both** a still and a video are
    considered Live Photo pairs; entries with only one side are ignored during
    pairing (they will be processed normally by ``_ingest_one``).
    """
    # Two-pass: first collect all stills/videos keyed by (dir, stem_lower).
    candidates: dict[tuple[str, str], dict[str, str]] = {}

    for name in all_names:
        p = PurePosixPath(name)
        ext_lower = p.suffix.lower()
        dir_str = str(p.parent)
        dir_prefix = "" if dir_str == "." else dir_str
        stem_lower = p.stem.lower()

        if ext_lower in _LIVE_STILL_EXTS:
            candidates.setdefault((dir_prefix, stem_lower), {})["still"] = name
        elif ext_lower in _LIVE_VIDEO_EXTS:
            candidates.setdefault((dir_prefix, stem_lower), {})["video"] = name

    # Keep only entries that have both sides.
    return {
        key: sides
        for key, sides in candidates.items()
        if "still" in sides and "video" in sides
    }


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
                # Map lowercase entry name → actual entry name for O(1) case-insensitive lookup.
                sidecar_map = {n.lower(): n for n in all_names if n.lower().endswith(_SIDECAR_EXT)}

                # Pre-scan for album metadata.json and per-photo timestamps.
                album_index = _build_album_index(zf)

                # Pre-scan for HEIC+MP4/MOV Live Photo pairs sharing the same stem/dir.
                live_photo_pairs = _build_live_photo_pairs(all_names)

                # Build a set of companion video names for fast O(1) lookup so that
                # _ingest_one can skip them when they belong to a Live Photo pair.
                paired_video_names: set[str] = {
                    sides["video"] for sides in live_photo_pairs.values()
                }

                job.total = len(media_names)
                job.status = ImportJobStatus.processing
                await session.commit()
                await _set_rls(session, owner_id)

                for media_name in media_names:
                    await _ingest_one(
                        session, job, owner_id, zf, media_name, sidecar_map, album_index,
                        live_photo_pairs=live_photo_pairs,
                        paired_video_names=paired_video_names,
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


def _read_sidecar_from_path(file_path: Path) -> dict | None:
    """Find and return the raw sidecar JSON dict for *file_path* on disk, or None.

    Tries the standard '.json' name, the '.supplemental-metadata.json' variant,
    and the live-photo companion (HEIC/HEIF/JPEG sidecar for an MP4/MOV).
    """
    _stem = PurePosixPath(file_path.name).stem
    candidates: list[str] = [
        _sidecar_name(file_path.name),
        file_path.name + _SUPPLEMENTAL_SIDECAR_EXT,
    ]
    for _photo_ext in (".heic", ".heif", ".jpg", ".jpeg"):
        candidates.append(_stem + _photo_ext + _SUPPLEMENTAL_SIDECAR_EXT)
        candidates.append(_stem + _photo_ext + _SIDECAR_EXT)
    for candidate_name in candidates:
        candidate_path = file_path.parent / candidate_name
        try:
            return json.loads(candidate_path.read_text("utf-8", errors="replace"))
        except FileNotFoundError:
            continue
        except Exception as exc:
            logger.warning("Could not read sidecar %s: %s", candidate_path, exc)
            break
    return None


def _read_sidecar(
    media_name: str,
    zf: zipfile.ZipFile,
    sidecar_map: dict[str, str],
) -> dict | None:
    """Find and return the raw sidecar JSON dict for *media_name*, or None.

    Tries the standard '.json' name, the '.supplemental-metadata.json' variant,
    and the live-photo companion (HEIC/HEIF/JPEG sidecar for an MP4/MOV).
    All comparisons are case-insensitive; *sidecar_map* maps lowercase→actual name.
    """
    _p = PurePosixPath(media_name)
    _stem = _p.stem
    _dir = str(_p.parent)
    _dir_prefix = "" if _dir == "." else _dir + "/"
    candidates: list[str] = [
        _sidecar_name(media_name),
        media_name + _SUPPLEMENTAL_SIDECAR_EXT,
    ]
    for _photo_ext in (".heic", ".heif", ".jpg", ".jpeg"):
        _companion = _dir_prefix + _stem + _photo_ext
        candidates.append(_companion + _SUPPLEMENTAL_SIDECAR_EXT)
        candidates.append(_companion + _SIDECAR_EXT)
    for candidate in candidates:
        actual = sidecar_map.get(candidate.lower())
        if actual is not None:
            try:
                raw_bytes = zf.read(actual)
                if raw_bytes:
                    return json.loads(raw_bytes.decode("utf-8", errors="replace"))
            except Exception as exc:
                logger.warning("Could not read sidecar %s: %s", actual, exc)
            break
    return None


async def _fix_duplicate_date(
    session: AsyncSession,
    asset_id: uuid.UUID,
    sidecar_data: dict | None,
    context: str,
) -> None:
    """Update captured_at on an already-imported asset if the sidecar has a better date."""
    if sidecar_data is None:
        return
    parsed = parse_sidecar(sidecar_data)
    if parsed is None:
        return
    canonical = merge_metadata(None, parsed)
    if canonical.captured_at is None:
        return
    asset = await session.get(MediaAsset, asset_id)
    if asset is not None:
        asset.captured_at = canonical.captured_at
        logger.debug("Updated captured_at for duplicate %s from sidecar", context)


async def _ingest_one(
    session: AsyncSession,
    job: ImportJob,
    owner_id: uuid.UUID,
    zf: zipfile.ZipFile,
    media_name: str,
    sidecar_map: dict[str, str],
    album_index: _AlbumIndex | None = None,
    *,
    live_photo_pairs: dict[tuple[str, str], dict[str, str]] | None = None,
    paired_video_names: set[str] | None = None,
) -> None:
    """Ingest a single media file from the zip.  Errors are caught and appended to job.errors.

    When *live_photo_pairs* and *paired_video_names* are provided (zip import path only):
      - Video files that are companions in a Live Photo pair are skipped here; they
        are ingested as part of the matching still file's processing.
      - Still files (HEIC/JPG) that have a matching companion video are ingested as a
        single Live Photo asset (one DB row, two S3 objects).
    """
    import contextlib
    from app.services.storage import StorageError

    # --- Live Photo companion video: skip — ingested as part of the still ---
    if paired_video_names and media_name in paired_video_names:
        logger.debug(
            "Skipping companion video %s — will be ingested with its Live Photo still",
            media_name,
        )
        return

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
        await _fix_duplicate_date(session, existing, _read_sidecar(media_name, zf, sidecar_map), media_name)
        logger.debug("Skipping duplicate %s (checksum %s)", media_name, checksum)
        job.duplicates += 1
        return

    staged_key: str | None = None
    staged_live_key: str | None = None
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

            # --- Live Photo companion video upload ---
            # Check if this still has a paired companion video in the same dir.
            live_video_size: int = 0
            _p = PurePosixPath(media_name)
            _dir_str = str(_p.parent)
            _dir_prefix = "" if _dir_str == "." else _dir_str
            _stem_lower = _p.stem.lower()
            _pair_key = (_dir_prefix, _stem_lower)
            _pair_sides = (live_photo_pairs or {}).get(_pair_key)
            if _pair_sides and "video" in _pair_sides:
                video_entry_name = _pair_sides["video"]
                video_zi = zf.getinfo(video_entry_name)
                live_video_size = video_zi.file_size
                live_video_suffix = Path(video_entry_name).suffix.lower()
                live_video_mime = (
                    "video/mp4" if live_video_suffix == ".mp4" else "video/quicktime"
                )
                with zf.open(video_entry_name) as _vid_obj:
                    staged_live_key = storage_service.upload_live_video(
                        str(owner_id),
                        str(asset_id),
                        _vid_obj,
                        live_video_suffix,
                        live_video_mime,
                    )
                logger.info(
                    "Live Photo pair: still=%s video=%s", media_name, video_entry_name
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
            if staged_live_key is not None:
                asset.is_live_photo = True
                asset.live_video_key = staged_live_key
            session.add(asset)
            total_bytes = len(data) + live_video_size
            await session.execute(
                text(
                    "UPDATE users"
                    " SET storage_used_bytes = storage_used_bytes + :delta"
                    " WHERE id = :uid"
                ),
                {"delta": total_bytes, "uid": owner_id},
            )
            await session.flush()

            # EXIF extraction
            exif_result = extract_exif(data, mime)

            # Sidecar lookup
            sidecar_data = _read_sidecar(media_name, zf, sidecar_map)
            parsed_sidecar = parse_sidecar(sidecar_data) if sidecar_data else None
            sidecar_found = parsed_sidecar is not None or sidecar_data is not None

            if not sidecar_found:
                asset.sidecar_missing = True
                job.no_sidecar += 1

            # Resolve captured_at via the canonical merge strategy
            canonical = merge_metadata(exif_result, parsed_sidecar)
            if canonical.captured_at is not None:
                captured = canonical.captured_at
                # Correct a wrong year using the "Photos from YYYY" folder name
                # (Google Takeout convention). Applied regardless of sidecar presence
                # because Google controls these folder names and they are reliable.
                year = _folder_year(media_name)
                if year and year != captured.year:
                    try:
                        captured = captured.replace(year=year)
                    except ValueError:
                        pass  # e.g. Feb 29 in a non-leap year — keep original
                asset.captured_at = captured
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
                album_id = await _ensure_album_path(session, owner_id, folder_path, album_index)
                if album_id is not None:
                    sort_order = album_index.sort_orders.get(media_name, 0) if album_index else 0
                    await _link_asset_to_album(session, album_id, asset_id, sort_order)

            await session.flush()

        # Dispatch thumbnail generation outside the savepoint so it only runs
        # when the DB write has succeeded.
        from app.worker.thumbnail_tasks import generate_thumbnails
        generate_thumbnails.delay(str(asset_id), str(owner_id))
        if parsed_sidecar is not None and parsed_sidecar.has_geo:
            from app.worker.geocode_tasks import resolve_asset_geocode
            resolve_asset_geocode.delay(
                str(asset_id), str(owner_id),
                parsed_sidecar.latitude, parsed_sidecar.longitude,
            )

    except Exception as exc:
        logger.warning("Failed to ingest %s: %s", media_name, exc)
        # Clean up staged S3 objects if they were uploaded before the DB write failed
        if staged_key is not None:
            with contextlib.suppress(StorageError):
                storage_service.delete(staged_key)
        if staged_live_key is not None:
            with contextlib.suppress(StorageError):
                storage_service.delete(staged_live_key)
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
        await _fix_duplicate_date(session, existing, _read_sidecar_from_path(file_path), file_path.name)
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

            # Sidecar lookup (standard, supplemental-metadata, live-photo companion).
            sidecar_data = _read_sidecar_from_path(file_path)
            parsed_sidecar = parse_sidecar(sidecar_data) if sidecar_data else None
            sidecar_found = parsed_sidecar is not None or sidecar_data is not None

            if not sidecar_found:
                asset.sidecar_missing = True
                job.no_sidecar += 1

            canonical = merge_metadata(exif_result, parsed_sidecar)
            if canonical.captured_at is not None:
                captured = canonical.captured_at
                # Correct a wrong year using the "Photos from YYYY" folder name
                # (Google Takeout convention). Applied regardless of sidecar presence
                # because Google controls these folder names and they are reliable.
                year = _folder_year(str(file_path))
                if year and year != captured.year:
                    try:
                        captured = captured.replace(year=year)
                    except ValueError:
                        pass  # e.g. Feb 29 in a non-leap year — keep original
                asset.captured_at = captured
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

        # Dispatch thumbnail generation outside the savepoint so it only runs
        # when the DB write has succeeded.
        from app.worker.thumbnail_tasks import generate_thumbnails
        generate_thumbnails.delay(str(asset_id), str(owner_id))
        if parsed_sidecar is not None and parsed_sidecar.has_geo:
            from app.worker.geocode_tasks import resolve_asset_geocode
            resolve_asset_geocode.delay(
                str(asset_id), str(owner_id),
                parsed_sidecar.latitude, parsed_sidecar.longitude,
            )

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
