"""Celery task for generating WebP thumbnails from media assets (issue #23).

Three outputs are produced per asset:
  thumb   — 320×320 fit-within, stored at {user_id}/thumbnails/{asset_id}/thumb.webp
  preview — 1280×1280 fit-within, stored at {user_id}/thumbnails/{asset_id}/preview.webp
  display — full original resolution WebP, stored at {user_id}/thumbnails/{asset_id}/display.webp
            (HEIC/HEIF images only — browsers cannot render HEIC natively)

For images: Pillow is used to resize/convert and save as WebP without EXIF.
For videos: ffmpeg extracts the first frame via subprocess, then Pillow resizes it.

The task retries up to 3 times on failure; after all retries are exhausted the
asset's thumbnail_error flag is set to true so the UI can show a broken-thumbnail
indicator.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import subprocess
import tempfile
import uuid

from celery.exceptions import MaxRetriesExceededError
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.media import MediaAsset
from app.services.storage import StorageError, storage_service
from app.worker.celery_app import celery_app

logger = logging.getLogger(__name__)

_THUMB_SIZE = (320, 320)
_PREVIEW_SIZE = (1280, 1280)
_HEIC_MIMES = {"image/heic", "image/heif"}


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------


def _resize_to_webp(img: Image.Image, max_size: tuple[int, int]) -> bytes:
    """Resize *img* to fit within *max_size* and return WebP bytes with no EXIF."""
    resized = img.copy()
    resized.thumbnail(max_size, Image.LANCZOS)
    buf = io.BytesIO()
    resized.save(buf, format="WEBP")
    return buf.getvalue()


def _generate_image_thumbnails(data: bytes) -> tuple[bytes, bytes]:
    """Return (thumb_webp, preview_webp) for a photo asset."""
    img = Image.open(io.BytesIO(data))
    # Convert palette/RGBA modes so WebP save works uniformly
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    return _resize_to_webp(img, _THUMB_SIZE), _resize_to_webp(img, _PREVIEW_SIZE)


def _to_display_webp(data: bytes) -> bytes:
    """Convert image data to full-resolution WebP without EXIF.

    No resizing — preserves all original pixels for use as the detail-view
    source on browsers that cannot render HEIC natively.
    """
    img = Image.open(io.BytesIO(data))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=90)
    return buf.getvalue()


def _generate_video_thumbnails(data: bytes) -> tuple[bytes, bytes]:
    """Return (thumb_webp, preview_webp) for a video asset.

    ffmpeg extracts the first frame; Pillow then resizes to both target sizes.
    Raises subprocess.CalledProcessError or OSError on ffmpeg failure.
    """
    with tempfile.TemporaryDirectory() as tmp:
        in_path = os.path.join(tmp, "input.bin")
        frame_path = os.path.join(tmp, "frame.png")

        with open(in_path, "wb") as f:
            f.write(data)

        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", in_path,
                "-vframes", "1",
                "-f", "image2",
                frame_path,
            ],
            check=True,
            capture_output=True,
        )

        img = Image.open(frame_path)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        return _resize_to_webp(img, _THUMB_SIZE), _resize_to_webp(img, _PREVIEW_SIZE)


# ---------------------------------------------------------------------------
# Async DB helpers
# ---------------------------------------------------------------------------


async def _set_thumbnail_ready(asset_id: uuid.UUID, owner_id: uuid.UUID) -> None:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await _set_rls(session, owner_id)
            await session.execute(
                text(
                    "UPDATE media_assets"
                    " SET thumbnail_ready = true, thumbnail_error = false"
                    " WHERE id = :asset_id"
                ),
                {"asset_id": asset_id},
            )
            await session.commit()
    finally:
        await engine.dispose()


async def _set_thumbnail_error(asset_id: uuid.UUID, owner_id: uuid.UUID) -> None:
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await _set_rls(session, owner_id)
            await session.execute(
                text(
                    "UPDATE media_assets"
                    " SET thumbnail_error = true"
                    " WHERE id = :asset_id"
                ),
                {"asset_id": asset_id},
            )
            await session.commit()
    finally:
        await engine.dispose()


async def _get_asset(
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


async def _set_rls(session: AsyncSession, owner_id: uuid.UUID) -> None:
    await session.execute(
        text(f"SET LOCAL app.current_user_id = '{owner_id}'")
    )


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


def _upload_thumbnail(user_id: str, asset_id: str, name: str, data: bytes) -> None:
    """Upload a WebP thumbnail under {user_id}/thumbnails/{asset_id}/{name}.webp."""
    key = f"{user_id}/thumbnails/{asset_id}/{name}.webp"
    storage_service._client.put_object(
        Bucket=storage_service._bucket,
        Key=key,
        Body=data,
        ContentType="image/webp",
    )
    logger.debug("Uploaded thumbnail %r", key)


# ---------------------------------------------------------------------------
# Celery task
# ---------------------------------------------------------------------------


@celery_app.task(
    name="thumbnails.generate",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
)
def generate_thumbnails(self, asset_id: str, owner_id: str) -> None:
    """Generate thumb and preview WebP thumbnails for a media asset.

    Downloads the original from MinIO, generates two WebP sizes, uploads them,
    and sets thumbnail_ready=true on success.  Retries up to 3 times on any
    error; sets thumbnail_error=true after all retries are exhausted.
    """
    asset_uuid = uuid.UUID(asset_id)
    owner_uuid = uuid.UUID(owner_id)

    try:
        result = asyncio.run(_get_asset(asset_uuid, owner_uuid))
        if result is None:
            logger.error("Asset %s not found — skipping thumbnail generation", asset_id)
            return

        storage_key, mime_type = result

        # Download original to memory
        response = storage_service._client.get_object(
            Bucket=storage_service._bucket,
            Key=storage_key,
        )
        data = response["Body"].read()

        # Generate thumbnails
        if mime_type.startswith("video/"):
            thumb_bytes, preview_bytes = _generate_video_thumbnails(data)
        else:
            thumb_bytes, preview_bytes = _generate_image_thumbnails(data)

        # Upload thumb and preview
        _upload_thumbnail(owner_id, asset_id, "thumb", thumb_bytes)
        _upload_thumbnail(owner_id, asset_id, "preview", preview_bytes)

        # For HEIC/HEIF, also generate a full-resolution display WebP so the
        # detail view can show the original quality in browsers that cannot
        # render HEIC natively.
        if mime_type in _HEIC_MIMES:
            display_bytes = _to_display_webp(data)
            _upload_thumbnail(owner_id, asset_id, "display", display_bytes)

        # Mark success
        asyncio.run(_set_thumbnail_ready(asset_uuid, owner_uuid))
        logger.info("Thumbnails generated for asset %s", asset_id)

    except MaxRetriesExceededError:
        logger.error("Thumbnail generation permanently failed for asset %s", asset_id)
        asyncio.run(_set_thumbnail_error(asset_uuid, owner_uuid))

    except Exception as exc:
        logger.warning(
            "Thumbnail generation failed for asset %s (attempt %d): %s",
            asset_id,
            self.request.retries + 1,
            exc,
        )
        try:
            raise self.retry(exc=exc)
        except MaxRetriesExceededError:
            logger.error("Thumbnail generation permanently failed for asset %s", asset_id)
            asyncio.run(_set_thumbnail_error(asset_uuid, owner_uuid))


# ---------------------------------------------------------------------------
# Backfill task
# ---------------------------------------------------------------------------


async def _get_heic_assets_for_user(owner_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    """Return (asset_id, storage_key) for all thumbnail_ready HEIC/HEIF assets owned by user."""
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            await _set_rls(session, owner_id)
            rows = await session.execute(
                select(MediaAsset.id, MediaAsset.storage_key).where(
                    MediaAsset.owner_id == owner_id,
                    MediaAsset.mime_type.in_(list(_HEIC_MIMES)),
                    MediaAsset.thumbnail_ready.is_(True),
                )
            )
            return list(rows)
    finally:
        await engine.dispose()


@celery_app.task(name="thumbnails.backfill_display_webp_user")
def backfill_display_webp_user(owner_id: str) -> None:
    """Generate display.webp for all thumbnail_ready HEIC/HEIF assets owned by a user.

    Idempotent — overwrites any existing display.webp.  Called by the
    POST /admin/backfill-display-webp endpoint for each user.
    """
    owner_uuid = uuid.UUID(owner_id)
    assets = asyncio.run(_get_heic_assets_for_user(owner_uuid))
    logger.info("Backfilling display.webp for %d HEIC assets (user %s)", len(assets), owner_id)
    for asset_id, storage_key in assets:
        try:
            response = storage_service._client.get_object(
                Bucket=storage_service._bucket,
                Key=storage_key,
            )
            data = response["Body"].read()
            display_bytes = _to_display_webp(data)
            _upload_thumbnail(owner_id, str(asset_id), "display", display_bytes)
            logger.info("display.webp generated for asset %s", asset_id)
        except Exception as exc:
            logger.warning("display.webp failed for asset %s: %s", asset_id, exc)
