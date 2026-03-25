"""EXIF metadata extraction from image and video files.

Two public entry points:
  extract_exif(data, mime_type)                  — pure function, never raises.
  apply_exif(session, *, asset_id, result, ...)  — persists to DB.

Timestamp priority (enforced by the caller via sidecar_captured_at):
  Google Takeout sidecar photoTakenTime > EXIF DateTimeOriginal.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO

from PIL import Image
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.media import MediaAsset, MediaMetadata

logger = logging.getLogger(__name__)

# Register HEIC/HEIF support if pillow-heif is installed (added in #42).
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    _HEIF_AVAILABLE = True
except ImportError:
    _HEIF_AVAILABLE = False

# Pillow EXIF tag IDs
_TAG_MAKE = 271
_TAG_MODEL = 272
_TAG_DATETIME_ORIGINAL = 36867


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ExifResult:
    """EXIF fields extracted from a single media file."""

    make: str | None
    """Camera manufacturer, e.g. 'Apple'."""

    model: str | None
    """Camera model, e.g. 'iPhone 13'."""

    width_px: int | None
    height_px: int | None

    captured_at: datetime | None
    """From EXIF DateTimeOriginal, interpreted as UTC. None when absent or unreadable."""


# ---------------------------------------------------------------------------
# Pure extractor
# ---------------------------------------------------------------------------


def extract_exif(data: bytes, mime_type: str) -> ExifResult:
    """Extract EXIF metadata from *data*.

    Returns an all-None ExifResult for video files (no EXIF to read) and on any
    read or parse error (corrupt file, unsupported format, etc.).  Never raises.
    """
    if mime_type.startswith("video/"):
        return ExifResult(make=None, model=None, width_px=None, height_px=None, captured_at=None)

    try:
        img = Image.open(BytesIO(data))
        width, height = img.size

        raw_exif = img.getexif()
        make = _str_or_none(raw_exif.get(_TAG_MAKE))
        model = _str_or_none(raw_exif.get(_TAG_MODEL))
        captured_at = _parse_exif_datetime(raw_exif.get(_TAG_DATETIME_ORIGINAL))

        return ExifResult(
            make=make,
            model=model,
            width_px=width,
            height_px=height,
            captured_at=captured_at,
        )
    except Exception:
        logger.warning("EXIF extraction failed (mime_type=%s)", mime_type, exc_info=True)
        return ExifResult(make=None, model=None, width_px=None, height_px=None, captured_at=None)


# ---------------------------------------------------------------------------
# DB persistence
# ---------------------------------------------------------------------------


async def apply_exif(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    result: ExifResult,
    sidecar_captured_at: datetime | None = None,
) -> None:
    """Write *result* to the database for *asset_id*.

    Operations performed (all flushed together):
      1. Upsert a media_metadata row with make, model, width_px, height_px.
      2. Update media_assets.captured_at from EXIF only when *sidecar_captured_at*
         is None — sidecar timestamps always take priority.

    The caller owns commit/rollback.
    """
    stmt = (
        pg_insert(MediaMetadata)
        .values(
            asset_id=asset_id,
            make=result.make,
            model=result.model,
            width_px=result.width_px,
            height_px=result.height_px,
        )
        .on_conflict_do_update(
            index_elements=["asset_id"],
            set_={
                "make": result.make,
                "model": result.model,
                "width_px": result.width_px,
                "height_px": result.height_px,
            },
        )
    )
    await session.execute(stmt)

    if sidecar_captured_at is None and result.captured_at is not None:
        asset = await session.get(MediaAsset, asset_id)
        if asset is not None:
            asset.captured_at = result.captured_at
            session.add(asset)

    await session.flush()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _str_or_none(value: object) -> str | None:
    """Return stripped string or None for empty/missing values."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _parse_exif_datetime(value: object) -> datetime | None:
    """Parse an EXIF DateTimeOriginal string ('2021:08:14 10:00:00') as UTC."""
    if not value:
        return None
    try:
        dt = datetime.strptime(str(value), "%Y:%m:%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
