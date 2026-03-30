"""Celery tasks for reverse-geocoding location labels (issue #125).

Two tasks:

  geocode.resolve_asset
    Given an asset_id, owner_id, and GPS coordinates, calls Nominatim,
    then updates Location.display_name for that asset.  Retries up to 3 times.

  geocode.backfill_user
    Finds every Location row with display_name IS NULL for a given user
    and dispatches one geocode.resolve_asset task per row.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from geoalchemy2.functions import ST_X, ST_Y
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.media import Location
from app.services.geocoding import reverse_geocode
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Async DB helpers
# ---------------------------------------------------------------------------


async def _set_rls(session: AsyncSession, owner_id: uuid.UUID) -> None:
    await session.execute(text(f"SET LOCAL app.current_user_id = '{owner_id}'"))


async def _update_display_name(
    asset_id: uuid.UUID, owner_id: uuid.UUID, display_name: str
) -> None:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await _set_rls(session, owner_id)
            await session.execute(
                update(Location)
                .where(Location.asset_id == asset_id)
                .values(display_name=display_name)
            )
            await session.commit()
    finally:
        await engine.dispose()


async def _get_ungeocoded_locations(
    owner_id: uuid.UUID,
) -> list[tuple[uuid.UUID, float, float]]:
    """Return (asset_id, lat, lon) for location rows with display_name IS NULL."""
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await _set_rls(session, owner_id)
            rows = await session.execute(
                select(
                    Location.asset_id,
                    ST_Y(Location.point).label("lat"),
                    ST_X(Location.point).label("lon"),
                ).where(Location.display_name.is_(None))
            )
            return [(r.asset_id, float(r.lat), float(r.lon)) for r in rows]
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Celery tasks
# ---------------------------------------------------------------------------


@celery_app.task(
    name="geocode.resolve_asset",
    bind=True,
    max_retries=3,
    default_retry_delay=30,
)
def resolve_asset_geocode(
    self, asset_id: str, owner_id: str, lat: float, lon: float
) -> None:
    """Reverse-geocode (lat, lon) and write the city name to Location.display_name.

    Idempotent — the UPDATE is a no-op if display_name is already populated.
    Retries up to 3 times on network or DB failure.
    """
    try:
        name = reverse_geocode(lat, lon)
        if name is None:
            logger.warning(
                "Geocode: no result for asset %s (%.6f, %.6f)", asset_id, lat, lon
            )
            return
        asyncio.run(_update_display_name(uuid.UUID(asset_id), uuid.UUID(owner_id), name))
        logger.info("Geocode: asset %s → %r", asset_id, name)
    except Exception as exc:
        logger.warning(
            "Geocode: failed for asset %s (attempt %d): %s",
            asset_id,
            self.request.retries + 1,
            exc,
        )
        try:
            raise self.retry(exc=exc)
        except Exception:
            logger.error("Geocode: permanently failed for asset %s", asset_id)


@celery_app.task(name="geocode.backfill_user", bind=True, max_retries=0)
def backfill_user_geocode(self, owner_id: str) -> None:
    """Dispatch geocode.resolve_asset for every ungeocoded location owned by owner_id."""
    owner_uuid = uuid.UUID(owner_id)
    rows = asyncio.run(_get_ungeocoded_locations(owner_uuid))
    logger.info(
        "Geocode backfill: %d ungeocoded location(s) for user %s", len(rows), owner_id
    )
    for asset_id, lat, lon in rows:
        resolve_asset_geocode.delay(str(asset_id), owner_id, lat, lon)
    logger.info(
        "Geocode backfill: enqueued %d task(s) for user %s", len(rows), owner_id
    )
