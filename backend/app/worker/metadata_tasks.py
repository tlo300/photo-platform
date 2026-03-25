"""Celery tasks for metadata backfill (issue #88).

Two tasks:

  metadata.backfill_user
    Enqueues one metadata.backfill_asset task per media asset owned by a user.
    Called from the admin backfill endpoint; sets RLS per-user so queries
    return only that user's assets.

  metadata.backfill_asset
    Re-downloads the original file from storage, runs full EXIF / video
    metadata extraction, upserts media_metadata, and conditionally inserts
    a locations row when EXIF GPS is present and no location row yet exists.
    Never touches captured_at.  Safe to re-run (idempotent).
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from geoalchemy2.functions import ST_MakePoint
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.media import Location, MediaAsset
from app.services.exif import apply_exif, extract_exif
from app.services.storage import storage_service
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Async DB helpers
# ---------------------------------------------------------------------------


async def _set_rls(session: AsyncSession, owner_id: uuid.UUID) -> None:
    await session.execute(
        text(f"SET LOCAL app.current_user_id = '{owner_id}'")
    )


async def _get_asset_info(
    asset_id: uuid.UUID, owner_id: uuid.UUID
) -> tuple[str, str] | None:
    """Return (storage_key, mime_type) for the asset, or None if not found."""
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await _set_rls(session, owner_id)
            asset = await session.get(MediaAsset, asset_id)
            if asset is None:
                return None
            return asset.storage_key, asset.mime_type
    finally:
        await engine.dispose()


async def _has_location(asset_id: uuid.UUID, owner_id: uuid.UUID) -> bool:
    """Return True if a locations row already exists for asset_id."""
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await _set_rls(session, owner_id)
            row = await session.scalar(
                select(Location.id).where(Location.asset_id == asset_id)
            )
            return row is not None
    finally:
        await engine.dispose()


async def _apply_metadata(
    asset_id: uuid.UUID,
    owner_id: uuid.UUID,
    storage_key: str,
    mime_type: str,
    data: bytes,
) -> None:
    """Run extraction, upsert media_metadata, and conditionally insert location."""
    result = extract_exif(data, mime_type)

    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await _set_rls(session, owner_id)

            await apply_exif(session, asset_id=asset_id, result=result)

            # Insert location from EXIF GPS only when no location row exists.
            # This preserves sidecar-sourced location data.
            if result.gps_latitude is not None and result.gps_longitude is not None:
                existing = await session.scalar(
                    select(Location.id).where(Location.asset_id == asset_id)
                )
                if existing is None:
                    stmt = (
                        pg_insert(Location)
                        .values(
                            asset_id=asset_id,
                            point=ST_MakePoint(result.gps_longitude, result.gps_latitude),
                            altitude_metres=result.gps_altitude,
                        )
                        .on_conflict_do_nothing(index_elements=["asset_id"])
                    )
                    await session.execute(stmt)
                    logger.debug(
                        "Location inserted from EXIF GPS for asset %s (%.6f, %.6f)",
                        asset_id,
                        result.gps_latitude,
                        result.gps_longitude,
                    )

            await session.commit()
    finally:
        await engine.dispose()


async def _get_user_asset_ids(owner_id: uuid.UUID) -> list[uuid.UUID]:
    """Return all asset IDs owned by owner_id."""
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await _set_rls(session, owner_id)
            rows = await session.scalars(
                select(MediaAsset.id).where(MediaAsset.owner_id == owner_id)
            )
            return list(rows)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Celery tasks
# ---------------------------------------------------------------------------


@celery_app.task(name="metadata.backfill_user", bind=True, max_retries=0)
def backfill_user_metadata(self, owner_id: str) -> None:
    """Enqueue one metadata.backfill_asset task per asset owned by owner_id.

    Logs the count of enqueued tasks.
    """
    owner_uuid = uuid.UUID(owner_id)
    asset_ids = asyncio.run(_get_user_asset_ids(owner_uuid))
    count = len(asset_ids)
    logger.info("Backfill: enqueueing %d asset(s) for user %s", count, owner_id)

    for asset_id in asset_ids:
        backfill_asset_metadata.delay(str(asset_id), owner_id)

    logger.info("Backfill: enqueued %d asset task(s) for user %s", count, owner_id)


@celery_app.task(
    name="metadata.backfill_asset",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def backfill_asset_metadata(self, asset_id: str, owner_id: str) -> None:
    """Re-extract and persist full metadata for a single media asset.

    Downloads the original from storage, runs EXIF / video extraction,
    upserts media_metadata with all new fields, and inserts a locations row
    from EXIF GPS when no sidecar-sourced location row already exists.

    Never modifies captured_at.  Idempotent — safe to re-run.
    Errors are logged and the task retries up to 3 times.
    """
    asset_uuid = uuid.UUID(asset_id)
    owner_uuid = uuid.UUID(owner_id)

    try:
        info = asyncio.run(_get_asset_info(asset_uuid, owner_uuid))
        if info is None:
            logger.warning(
                "Backfill: asset %s not found for owner %s — skipping",
                asset_id, owner_id,
            )
            return

        storage_key, mime_type = info

        response = storage_service._client.get_object(
            Bucket=storage_service._bucket,
            Key=storage_key,
        )
        data: bytes = response["Body"].read()

        asyncio.run(_apply_metadata(asset_uuid, owner_uuid, storage_key, mime_type, data))
        logger.info("Backfill: metadata updated for asset %s", asset_id)

    except Exception as exc:
        logger.warning(
            "Backfill: failed for asset %s (attempt %d): %s",
            asset_id,
            self.request.retries + 1,
            exc,
        )
        try:
            raise self.retry(exc=exc)
        except Exception:
            logger.error(
                "Backfill: permanently failed for asset %s after %d attempts",
                asset_id,
                self.max_retries,
            )
