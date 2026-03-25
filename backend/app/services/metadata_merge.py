"""Merge EXIF and Google Takeout sidecar metadata into a single canonical record.

Single public entry point:
  merge_metadata(exif, sidecar) -> CanonicalMetadata

This is a pure function (no I/O, no DB).  It is the authoritative source for
captured_at — the worker must use its result rather than letting apply_exif or
apply_sidecar race to set the timestamp independently.

Priority table
--------------
Field            Primary source               Fallback
captured_at      sidecar photoTakenTime       EXIF DateTimeOriginal (sanity-checked)
latitude/lon     sidecar geoData (non-zero)   None
altitude         sidecar geoData.altitude     None
description      sidecar description          None
people           sidecar people[].name        None
make / model     EXIF                         None
width / height   EXIF                         None
raw_json         sidecar verbatim             None

Sanity checks applied to the EXIF date before it can be used as a fallback:
  - Year outside 1990-2030: treated as absent, logged at WARNING.
  - Year differs from sidecar year by more than 2: JSON wins, logged at WARNING.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from app.services.exif import ExifResult
from app.services.takeout_sidecar import ParsedSidecar

logger = logging.getLogger(__name__)

# Inclusive bounds for acceptable EXIF capture years.
_EXIF_YEAR_MIN = 1990
_EXIF_YEAR_MAX = 2030

# If EXIF and sidecar years differ by more than this, the sidecar wins.
_MAX_YEAR_DIFF = 2


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class CanonicalMetadata:
    """Single authoritative metadata record for one media asset."""

    # Timestamp ---
    captured_at: datetime | None
    """Resolved capture timestamp.  None when no trustworthy source exists."""

    # Camera ---
    make: str | None
    model: str | None
    width_px: int | None
    height_px: int | None

    # Location ---
    latitude: float | None
    longitude: float | None
    altitude_metres: float | None
    has_geo: bool

    # Descriptive ---
    description: str | None
    people: list[str] = field(default_factory=list)

    # Raw sidecar ---
    raw_json: dict | None = None


# ---------------------------------------------------------------------------
# Pure merge function
# ---------------------------------------------------------------------------


def merge_metadata(
    exif: ExifResult | None,
    sidecar: ParsedSidecar | None,
) -> CanonicalMetadata:
    """Merge EXIF and sidecar data into one canonical record.

    Neither argument is required — all combinations (both, either, neither)
    produce a valid CanonicalMetadata.  Never raises.
    """
    sidecar_ts = sidecar.captured_at if sidecar is not None else None
    exif_ts = exif.captured_at if exif is not None else None

    captured_at = _resolve_captured_at(exif_ts, sidecar_ts)

    return CanonicalMetadata(
        captured_at=captured_at,
        make=exif.make if exif is not None else None,
        model=exif.model if exif is not None else None,
        width_px=exif.width_px if exif is not None else None,
        height_px=exif.height_px if exif is not None else None,
        latitude=sidecar.latitude if sidecar is not None else None,
        longitude=sidecar.longitude if sidecar is not None else None,
        altitude_metres=sidecar.altitude_metres if sidecar is not None else None,
        has_geo=sidecar.has_geo if sidecar is not None else False,
        description=sidecar.description if sidecar is not None else None,
        people=list(sidecar.people) if sidecar is not None else [],
        raw_json=sidecar.raw if sidecar is not None else None,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_captured_at(
    exif_ts: datetime | None,
    sidecar_ts: datetime | None,
) -> datetime | None:
    """Return the canonical capture timestamp given the two raw sources.

    Decision logic (in order):
      1. Validate the EXIF timestamp — discard it if the year is out of range.
      2. If both sources survive validation and their years differ by > MAX_YEAR_DIFF,
         the sidecar wins and the discrepancy is logged.
      3. Sidecar timestamp takes priority over EXIF.
    """
    validated_exif = _validate_exif_year(exif_ts)

    if validated_exif is not None and sidecar_ts is not None:
        year_diff = abs(validated_exif.year - sidecar_ts.year)
        if year_diff > _MAX_YEAR_DIFF:
            logger.warning(
                "EXIF year %d and sidecar year %d differ by %d — using sidecar",
                validated_exif.year,
                sidecar_ts.year,
                year_diff,
            )
            return sidecar_ts

    # Normal priority: sidecar first, then EXIF.
    return sidecar_ts if sidecar_ts is not None else validated_exif


def _validate_exif_year(ts: datetime | None) -> datetime | None:
    """Return *ts* if its year is within the acceptable range, else None."""
    if ts is None:
        return None
    if not (_EXIF_YEAR_MIN <= ts.year <= _EXIF_YEAR_MAX):
        logger.warning(
            "EXIF DateTimeOriginal year %d is outside %d–%d — discarding",
            ts.year,
            _EXIF_YEAR_MIN,
            _EXIF_YEAR_MAX,
        )
        return None
    return ts
