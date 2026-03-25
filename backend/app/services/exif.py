"""EXIF metadata extraction from image and video files.

Two public entry points:
  extract_exif(data, mime_type)            — pure function, never raises.
  apply_exif(session, *, asset_id, result) — persists to DB.

apply_exif writes make/model/dimensions/duration and extended EXIF fields
(iso, aperture, shutter_speed, focal_length, flash) to media_metadata.
GPS coordinates extracted from EXIF are stored in ExifResult but NOT written
to the locations table here — that is handled by the caller (backfill task)
so that sidecar-sourced location data is never silently overwritten.

captured_at is owned by the caller (via merge_metadata) — apply_exif never
touches media_assets.captured_at.
"""

import json
import logging
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO

from PIL import Image
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.media import MediaMetadata

logger = logging.getLogger(__name__)

# Register HEIC/HEIF support if pillow-heif is installed (added in #42).
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    _HEIF_AVAILABLE = True
except ImportError:
    _HEIF_AVAILABLE = False

# Pillow EXIF tag IDs — top-level IFD0
_TAG_MAKE = 271
_TAG_MODEL = 272

# Exif sub-IFD (tag 34665) entries
_TAG_DATETIME_ORIGINAL = 36867
_TAG_EXPOSURE_TIME = 33434
_TAG_FNUMBER = 33437
_TAG_ISO_SPEED = 34855
_TAG_FLASH = 37385
_TAG_FOCAL_LENGTH = 37386

# GPS sub-IFD (tag 34853) entries
_GPS_IFD_TAG = 34853
_GPS_LAT_REF = 1
_GPS_LAT = 2
_GPS_LON_REF = 3
_GPS_LON = 4
_GPS_ALT_REF = 5
_GPS_ALT = 6


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ExifResult:
    """Metadata fields extracted from a single media file.

    All fields default to None so that callers that only care about a subset
    of fields (e.g. merge_metadata, which only uses make/model/captured_at)
    can construct ExifResult without specifying every new field.
    """

    make: str | None = None
    """Camera manufacturer, e.g. 'Apple'."""

    model: str | None = None
    """Camera model, e.g. 'iPhone 13'."""

    width_px: int | None = None
    height_px: int | None = None

    captured_at: datetime | None = None
    """From EXIF DateTimeOriginal, interpreted as UTC. None when absent or unreadable."""

    # Extended camera settings (images only)
    iso: int | None = None
    """ISO speed rating."""

    aperture: float | None = None
    """f-number, e.g. 2.8 for f/2.8."""

    shutter_speed: float | None = None
    """Exposure time in seconds, e.g. 0.004 for 1/250 s."""

    focal_length: float | None = None
    """Focal length in mm."""

    flash: bool | None = None
    """True if the flash fired, False if it did not, None if not recorded."""

    # GPS from EXIF (images only — populated when no sidecar provides geo data)
    gps_latitude: float | None = None
    gps_longitude: float | None = None
    gps_altitude: float | None = None

    # Video metadata
    duration_seconds: float | None = None
    """Duration in seconds (videos); None for images."""


def _all_none_result() -> ExifResult:
    return ExifResult()


# ---------------------------------------------------------------------------
# Pure extractor
# ---------------------------------------------------------------------------


def extract_exif(data: bytes, mime_type: str) -> ExifResult:
    """Extract metadata from *data*.

    For images: reads EXIF tags via Pillow (make, model, dimensions,
    captured_at, ISO, aperture, shutter speed, focal length, flash, GPS).
    For videos: uses ffprobe to extract duration and resolution.

    Returns an all-None ExifResult on any read or parse error.  Never raises.
    """
    if mime_type.startswith("video/"):
        return _extract_video(data)

    try:
        img = Image.open(BytesIO(data))
        width, height = img.size

        raw_exif = img.getexif()
        exif_ifd = raw_exif.get_ifd(34665)
        gps_ifd = raw_exif.get_ifd(_GPS_IFD_TAG)

        make = _str_or_none(raw_exif.get(_TAG_MAKE))
        model = _str_or_none(raw_exif.get(_TAG_MODEL))
        captured_at = _parse_exif_datetime(
            exif_ifd.get(_TAG_DATETIME_ORIGINAL) or raw_exif.get(_TAG_DATETIME_ORIGINAL)
        )

        iso = _iso_or_none(exif_ifd.get(_TAG_ISO_SPEED))
        aperture = _rational_to_float(exif_ifd.get(_TAG_FNUMBER))
        shutter_speed = _rational_to_float(exif_ifd.get(_TAG_EXPOSURE_TIME))
        focal_length = _rational_to_float(exif_ifd.get(_TAG_FOCAL_LENGTH))
        flash = _flash_or_none(exif_ifd.get(_TAG_FLASH))
        gps_lat, gps_lon, gps_alt = _parse_gps(gps_ifd)

        return ExifResult(
            make=make,
            model=model,
            width_px=width,
            height_px=height,
            captured_at=captured_at,
            iso=iso,
            aperture=aperture,
            shutter_speed=shutter_speed,
            focal_length=focal_length,
            flash=flash,
            gps_latitude=gps_lat,
            gps_longitude=gps_lon,
            gps_altitude=gps_alt,
            duration_seconds=None,
        )
    except Exception:
        logger.warning("EXIF extraction failed (mime_type=%s)", mime_type, exc_info=True)
        return _all_none_result()


def _extract_video(data: bytes) -> ExifResult:
    """Use ffprobe to extract duration, width, and height from video bytes."""
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(data)
            tmp_path = f.name

        proc = subprocess.run(
            [
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                tmp_path,
            ],
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0:
            return _all_none_result()

        info = json.loads(proc.stdout)
        for stream in info.get("streams", []):
            if stream.get("codec_type") == "video":
                width = stream.get("width")
                height = stream.get("height")
                duration_str = stream.get("duration")
                duration = float(duration_str) if duration_str else None
                return ExifResult(
                    make=None,
                    model=None,
                    width_px=int(width) if width else None,
                    height_px=int(height) if height else None,
                    captured_at=None,
                    iso=None,
                    aperture=None,
                    shutter_speed=None,
                    focal_length=None,
                    flash=None,
                    gps_latitude=None,
                    gps_longitude=None,
                    gps_altitude=None,
                    duration_seconds=duration,
                )
        return _all_none_result()
    except Exception:
        logger.warning("Video metadata extraction failed", exc_info=True)
        return _all_none_result()
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# DB persistence
# ---------------------------------------------------------------------------


async def apply_exif(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    result: ExifResult,
) -> None:
    """Write *result* to the database for *asset_id*.

    Upserts make, model, width_px, height_px, duration_seconds, iso,
    aperture, shutter_speed, focal_length, and flash into media_metadata.

    GPS coordinates (gps_latitude/longitude/altitude) are intentionally
    excluded — GPS → locations upsert is handled by the caller so that
    sidecar-sourced location data is never overwritten.

    captured_at is set by the caller after merge_metadata() resolves
    priority.  The caller owns commit/rollback.
    """
    stmt = (
        pg_insert(MediaMetadata)
        .values(
            asset_id=asset_id,
            make=result.make,
            model=result.model,
            width_px=result.width_px,
            height_px=result.height_px,
            duration_seconds=result.duration_seconds,
            iso=result.iso,
            aperture=result.aperture,
            shutter_speed=result.shutter_speed,
            focal_length=result.focal_length,
            flash=result.flash,
        )
        .on_conflict_do_update(
            index_elements=["asset_id"],
            set_={
                "make": result.make,
                "model": result.model,
                "width_px": result.width_px,
                "height_px": result.height_px,
                "duration_seconds": result.duration_seconds,
                "iso": result.iso,
                "aperture": result.aperture,
                "shutter_speed": result.shutter_speed,
                "focal_length": result.focal_length,
                "flash": result.flash,
            },
        )
    )
    await session.execute(stmt)
    await session.flush()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _str_or_none(value: object) -> str | None:
    """Return stripped string or None for empty/missing values.

    Also strips null bytes, which some EXIF strings contain and PostgreSQL
    rejects with CharacterNotInRepertoireError.
    """
    if value is None:
        return None
    s = str(value).replace("\x00", "").strip()
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


def _rational_to_float(value: object) -> float | None:
    """Convert an EXIF rational to float.

    Accepts both IFDRational objects (returned by Pillow when reading real
    EXIF) and plain (numerator, denominator) tuples (used in test helpers
    and by some older EXIF parsers).
    """
    if value is None:
        return None
    try:
        if isinstance(value, tuple) and len(value) == 2:
            num, den = value
            if den == 0:
                return None
            return float(num) / float(den)
        f = float(value)
        return f if f == f else None  # guard against NaN
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _iso_or_none(value: object) -> int | None:
    """Extract ISO speed rating. The tag may contain a list of ints or a single int."""
    if value is None:
        return None
    try:
        if isinstance(value, (list, tuple)):
            value = value[0]
        return int(value)
    except (TypeError, ValueError, IndexError):
        return None


def _flash_or_none(value: object) -> bool | None:
    """Decode the EXIF Flash tag.  Bit 0 indicates whether the flash fired."""
    if value is None:
        return None
    try:
        return bool(int(value) & 1)
    except (TypeError, ValueError):
        return None


def _dms_to_decimal(dms: object, ref: object) -> float | None:
    """Convert a GPS (degrees, minutes, seconds) tuple and reference to decimal degrees.

    *dms* is a tuple of three values, each convertible to float via
    _rational_to_float.  *ref* is 'N', 'S', 'E', or 'W'.
    """
    if not dms or not ref:
        return None
    try:
        parts = list(dms)
        if len(parts) != 3:
            return None
        degrees = _rational_to_float(parts[0])
        minutes = _rational_to_float(parts[1])
        seconds = _rational_to_float(parts[2])
        if degrees is None or minutes is None or seconds is None:
            return None
        decimal = degrees + minutes / 60.0 + seconds / 3600.0
        if str(ref).upper() in ("S", "W"):
            decimal = -decimal
        return decimal
    except (TypeError, ValueError, IndexError):
        return None


def _parse_gps(gps_ifd: dict) -> tuple[float | None, float | None, float | None]:
    """Extract (latitude, longitude, altitude_metres) from a GPS sub-IFD dict.

    Returns (None, None, None) when GPS data is absent or invalid.
    Coordinates of (0.0, 0.0) — the null island — are treated as absent,
    consistent with apply_sidecar's has_geo check.
    """
    if not gps_ifd:
        return None, None, None

    lat = _dms_to_decimal(gps_ifd.get(_GPS_LAT), gps_ifd.get(_GPS_LAT_REF))
    lon = _dms_to_decimal(gps_ifd.get(_GPS_LON), gps_ifd.get(_GPS_LON_REF))

    if lat is None or lon is None or (lat == 0.0 and lon == 0.0):
        return None, None, None

    # Altitude: rational value + AltitudeRef (0=above sea level, 1=below)
    alt_raw = _rational_to_float(gps_ifd.get(_GPS_ALT))
    alt_ref = gps_ifd.get(_GPS_ALT_REF, 0)
    alt: float | None = None
    if alt_raw is not None:
        try:
            alt = -alt_raw if int(alt_ref) == 1 else alt_raw
        except (TypeError, ValueError):
            alt = alt_raw

    return lat, lon, alt
