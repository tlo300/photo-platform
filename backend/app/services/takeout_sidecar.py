"""Google Takeout JSON sidecar parser.

Two public entry points:
  parse_sidecar(raw)        — pure function, no DB, safe to call anywhere.
  apply_sidecar(...)        — persists the parsed result to the database.

Field priority: sidecar timestamps take precedence over embedded EXIF
(enforced by the caller; apply_sidecar always writes captured_at when present).
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from geoalchemy2.functions import ST_MakePoint
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.google_takeout import GoogleMetadataRaw
from app.models.media import Location, MediaAsset
from app.models.tag import AssetTag, Tag

logger = logging.getLogger(__name__)

_GOOGLE_PEOPLE_SOURCE = "google_people"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ParsedSidecar:
    """Extracted fields from a single Google Takeout JSON sidecar."""

    captured_at: datetime | None
    """Taken from photoTakenTime.timestamp (preferred) or creationTime.timestamp."""

    latitude: float | None
    longitude: float | None
    altitude_metres: float | None
    has_geo: bool
    """True when latitude and longitude are both present and non-zero."""

    description: str | None
    people: list[str] = field(default_factory=list)
    """Names from the people[] array."""

    raw: dict = field(default_factory=dict)
    """The original sidecar dict, stored verbatim."""


# ---------------------------------------------------------------------------
# Pure parser
# ---------------------------------------------------------------------------

def parse_sidecar(raw: dict) -> ParsedSidecar:
    """Parse a Google Takeout JSON sidecar dict into a ParsedSidecar.

    Never raises — missing or invalid fields are treated as absent.
    """
    captured_at = _parse_timestamp(raw, "photoTakenTime") or _parse_timestamp(
        raw, "creationTime"
    )

    geo = raw.get("geoData") or {}
    lat = _float_or_none(geo.get("latitude"))
    lon = _float_or_none(geo.get("longitude"))
    alt = _float_or_none(geo.get("altitude"))
    has_geo = lat is not None and lon is not None and (lat != 0.0 or lon != 0.0)

    description = raw.get("description") or None
    if description is not None:
        description = str(description).strip() or None

    people: list[str] = []
    for entry in raw.get("people") or []:
        name = (entry.get("name") or "").strip() if isinstance(entry, dict) else ""
        if name:
            people.append(name)

    return ParsedSidecar(
        captured_at=captured_at,
        latitude=lat if has_geo else None,
        longitude=lon if has_geo else None,
        altitude_metres=alt if has_geo else None,
        has_geo=has_geo,
        description=description,
        people=people,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# DB persistence
# ---------------------------------------------------------------------------

async def apply_sidecar(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    owner_id: uuid.UUID,
    parsed: ParsedSidecar,
) -> None:
    """Write a ParsedSidecar to the database for *asset_id*.

    Operations performed (all flushed together):
      1. Update media_assets.captured_at and .description.
      2. Upsert a locations row when has_geo is True.
      3. Upsert tags for each person with source='google_people'.
      4. Upsert google_metadata_raw with the verbatim JSON.

    The caller owns commit/rollback.
    """
    # 1. Update the asset row
    asset = await session.get(MediaAsset, asset_id)
    if asset is None:
        raise ValueError(f"Asset {asset_id!r} not found")

    if parsed.captured_at is not None:
        asset.captured_at = parsed.captured_at
    if parsed.description is not None:
        asset.description = parsed.description
    session.add(asset)

    # 2. Location row (upsert via ON CONFLICT DO UPDATE)
    if parsed.has_geo:
        stmt = (
            pg_insert(Location)
            .values(
                asset_id=asset_id,
                point=ST_MakePoint(parsed.longitude, parsed.latitude),
                altitude_metres=parsed.altitude_metres,
            )
            .on_conflict_do_update(
                index_elements=["asset_id"],
                set_={
                    "point": ST_MakePoint(parsed.longitude, parsed.latitude),
                    "altitude_metres": parsed.altitude_metres,
                },
            )
        )
        await session.execute(stmt)

    # 3. People → tags
    for name in parsed.people:
        # Upsert the tag row (owner_id + name is unique)
        tag_stmt = (
            pg_insert(Tag)
            .values(owner_id=owner_id, name=name)
            .on_conflict_do_nothing()
            .returning(Tag.id)
        )
        result = await session.execute(tag_stmt)
        row = result.fetchone()
        if row is None:
            # Tag already existed — look it up
            existing = await session.execute(
                select(Tag.id).where(Tag.owner_id == owner_id, Tag.name == name)
            )
            tag_id = existing.scalar_one()
        else:
            tag_id = row[0]

        # Upsert the asset↔tag relationship
        at_stmt = (
            pg_insert(AssetTag)
            .values(asset_id=asset_id, tag_id=tag_id, source=_GOOGLE_PEOPLE_SOURCE)
            .on_conflict_do_nothing()
        )
        await session.execute(at_stmt)

    # 4. Raw sidecar JSON
    raw_stmt = (
        pg_insert(GoogleMetadataRaw)
        .values(asset_id=asset_id, raw_json=parsed.raw)
        .on_conflict_do_update(
            index_elements=["asset_id"],
            set_={"raw_json": parsed.raw},
        )
    )
    await session.execute(raw_stmt)

    await session.flush()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_timestamp(raw: dict, key: str) -> datetime | None:
    """Extract a UTC datetime from raw[key]['timestamp'] (Unix epoch string)."""
    try:
        ts = int(raw[key]["timestamp"])
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OSError):
        return None


def _float_or_none(value: object) -> float | None:
    """Coerce *value* to float or return None on any failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
