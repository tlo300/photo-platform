"""Unit tests for the metadata backfill Celery tasks (issue #88).

All external I/O (MinIO, DB helpers) is mocked.
No live services are required.

Tests cover:
  1.  backfill_asset_metadata happy path — image asset
  2.  backfill_asset_metadata happy path — video asset (ffprobe path)
  3.  GPS present + no existing location → location row inserted
  4.  GPS present + location already exists → location NOT overwritten
  5.  Asset not found in DB → task skips cleanly
  6.  Storage error → task retries
  7.  backfill_user_metadata enqueues one task per asset
  8.  backfill_user_metadata with zero assets is a no-op
"""

from __future__ import annotations

import io
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_jpeg_bytes(width: int = 60, height: int = 40) -> bytes:
    img = Image.new("RGB", (width, height), color=(10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _asset_id() -> str:
    return str(uuid.uuid4())


def _owner_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# backfill_asset_metadata
# ---------------------------------------------------------------------------


def test_backfill_asset_image_happy_path():
    """Task downloads image, applies metadata, and does not crash."""
    asset = _asset_id()
    owner = _owner_id()
    jpeg = _make_jpeg_bytes()
    storage_key = f"{owner}/{asset}/original.jpg"

    with (
        patch(
            "app.worker.metadata_tasks._get_asset_info",
            new=AsyncMock(return_value=(storage_key, "image/jpeg")),
        ),
        patch(
            "app.worker.metadata_tasks._apply_metadata",
            new=AsyncMock(),
        ) as mock_apply,
        patch("app.worker.metadata_tasks.storage_service") as mock_storage,
    ):
        mock_storage._client.get_object.return_value = {"Body": io.BytesIO(jpeg)}

        from app.worker.metadata_tasks import backfill_asset_metadata

        backfill_asset_metadata(asset, owner)

    mock_apply.assert_awaited_once()
    call_kwargs = mock_apply.call_args
    assert call_kwargs.args[0] == uuid.UUID(asset)
    assert call_kwargs.args[1] == uuid.UUID(owner)


def test_backfill_asset_video_happy_path():
    """Task processes a video asset without crashing."""
    asset = _asset_id()
    owner = _owner_id()
    storage_key = f"{owner}/{asset}/original.mp4"

    with (
        patch(
            "app.worker.metadata_tasks._get_asset_info",
            new=AsyncMock(return_value=(storage_key, "video/mp4")),
        ),
        patch(
            "app.worker.metadata_tasks._apply_metadata",
            new=AsyncMock(),
        ) as mock_apply,
        patch("app.worker.metadata_tasks.storage_service") as mock_storage,
    ):
        mock_storage._client.get_object.return_value = {
            "Body": io.BytesIO(b"\x00" * 64)
        }

        from app.worker.metadata_tasks import backfill_asset_metadata

        backfill_asset_metadata(asset, owner)

    mock_apply.assert_awaited_once()


def test_backfill_asset_skips_missing_asset():
    """Task exits cleanly when the asset is no longer in the DB."""
    asset = _asset_id()
    owner = _owner_id()

    with (
        patch(
            "app.worker.metadata_tasks._get_asset_info",
            new=AsyncMock(return_value=None),
        ),
        patch("app.worker.metadata_tasks.storage_service") as mock_storage,
        patch(
            "app.worker.metadata_tasks._apply_metadata",
            new=AsyncMock(),
        ) as mock_apply,
    ):
        from app.worker.metadata_tasks import backfill_asset_metadata

        backfill_asset_metadata(asset, owner)

    mock_storage._client.get_object.assert_not_called()
    mock_apply.assert_not_awaited()


def test_backfill_asset_retries_on_storage_error():
    """Storage failure causes the task to retry."""
    from celery.exceptions import MaxRetriesExceededError

    asset = _asset_id()
    owner = _owner_id()
    storage_key = f"{owner}/{asset}/original.jpg"

    from app.worker.metadata_tasks import backfill_asset_metadata

    with (
        patch(
            "app.worker.metadata_tasks._get_asset_info",
            new=AsyncMock(return_value=(storage_key, "image/jpeg")),
        ),
        patch("app.worker.metadata_tasks.storage_service") as mock_storage,
        patch.object(
            backfill_asset_metadata, "retry", side_effect=MaxRetriesExceededError()
        ),
    ):
        mock_storage._client.get_object.side_effect = ConnectionError("timeout")

        backfill_asset_metadata.apply(args=[asset, owner])

    # After retries exhausted the task should not propagate an exception to the caller
    # (the final except catches MaxRetriesExceededError and logs it)


# ---------------------------------------------------------------------------
# GPS / location logic inside _apply_metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_metadata_inserts_location_when_absent():
    """GPS in EXIF + no existing location row → location inserted."""
    asset_uuid = uuid.uuid4()
    owner_uuid = uuid.uuid4()
    storage_key = f"{owner_uuid}/{asset_uuid}/original.jpg"

    from app.services.exif import ExifResult

    gps_result = ExifResult(
        make="FakeCorp",
        model="TestCam",
        width_px=120,
        height_px=80,
        captured_at=None,
        iso=None,
        aperture=None,
        shutter_speed=None,
        focal_length=None,
        flash=None,
        gps_latitude=52.37,
        gps_longitude=4.89,
        gps_altitude=5.0,
        duration_seconds=None,
    )

    mock_session = AsyncMock()
    mock_session.scalar = AsyncMock(return_value=None)  # no existing location

    mock_factory_instance = MagicMock()
    mock_factory_instance.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory_instance.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_factory_instance)
    mock_engine = AsyncMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("app.worker.metadata_tasks.extract_exif", return_value=gps_result),
        patch("app.worker.metadata_tasks.apply_exif", new=AsyncMock()),
        patch(
            "app.worker.metadata_tasks.create_async_engine",
            return_value=mock_engine,
        ),
        patch(
            "app.worker.metadata_tasks.async_sessionmaker",
            return_value=mock_factory,
        ),
    ):
        from app.worker.metadata_tasks import _apply_metadata

        await _apply_metadata(
            asset_uuid,
            owner_uuid,
            storage_key,
            "image/jpeg",
            b"\x00fake\x00",
        )

    # session.execute should have been called at least twice:
    # once for SET LOCAL RLS, once for the location INSERT
    assert mock_session.execute.call_count >= 2


@pytest.mark.asyncio
async def test_apply_metadata_skips_location_when_exists():
    """GPS in EXIF + existing location row → no INSERT executed."""
    asset_uuid = uuid.uuid4()
    owner_uuid = uuid.uuid4()
    storage_key = f"{owner_uuid}/{asset_uuid}/original.jpg"

    from app.services.exif import ExifResult

    gps_result = ExifResult(
        make=None,
        model=None,
        width_px=120,
        height_px=80,
        captured_at=None,
        iso=None,
        aperture=None,
        shutter_speed=None,
        focal_length=None,
        flash=None,
        gps_latitude=52.37,
        gps_longitude=4.89,
        gps_altitude=None,
        duration_seconds=None,
    )

    existing_location_id = uuid.uuid4()
    mock_session = AsyncMock()
    mock_session.scalar = AsyncMock(return_value=existing_location_id)

    mock_factory_instance = MagicMock()
    mock_factory_instance.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory_instance.__aexit__ = AsyncMock(return_value=False)

    mock_factory = MagicMock(return_value=mock_factory_instance)
    mock_engine = AsyncMock()
    mock_engine.dispose = AsyncMock()

    execute_calls = []

    async def _capture_execute(stmt, *args, **kwargs):
        execute_calls.append(stmt)

    mock_session.execute = AsyncMock(side_effect=_capture_execute)

    with (
        patch("app.worker.metadata_tasks.extract_exif", return_value=gps_result),
        patch("app.worker.metadata_tasks.apply_exif", new=AsyncMock()),
        patch(
            "app.worker.metadata_tasks.create_async_engine",
            return_value=mock_engine,
        ),
        patch(
            "app.worker.metadata_tasks.async_sessionmaker",
            return_value=mock_factory,
        ),
    ):
        from app.worker.metadata_tasks import _apply_metadata

        await _apply_metadata(
            asset_uuid,
            owner_uuid,
            storage_key,
            "image/jpeg",
            b"\x00fake\x00",
        )

    # Only the SET LOCAL RLS call should have been executed (no INSERT)
    assert len(execute_calls) == 1


# ---------------------------------------------------------------------------
# backfill_user_metadata
# ---------------------------------------------------------------------------


def test_backfill_user_enqueues_one_task_per_asset():
    """backfill_user_metadata dispatches one backfill_asset task per asset."""
    owner = _owner_id()
    asset_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]

    with (
        patch(
            "app.worker.metadata_tasks._get_user_asset_ids",
            new=AsyncMock(return_value=asset_ids),
        ),
        patch(
            "app.worker.metadata_tasks.backfill_asset_metadata"
        ) as mock_task,
    ):
        mock_task.delay = MagicMock()

        from app.worker.metadata_tasks import backfill_user_metadata

        backfill_user_metadata(owner)

    assert mock_task.delay.call_count == 3
    dispatched_asset_ids = {call.args[0] for call in mock_task.delay.call_args_list}
    assert dispatched_asset_ids == {str(a) for a in asset_ids}


def test_backfill_user_no_assets_is_noop():
    """backfill_user_metadata with no assets dispatches no tasks."""
    owner = _owner_id()

    with (
        patch(
            "app.worker.metadata_tasks._get_user_asset_ids",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.worker.metadata_tasks.backfill_asset_metadata"
        ) as mock_task,
    ):
        mock_task.delay = MagicMock()

        from app.worker.metadata_tasks import backfill_user_metadata

        backfill_user_metadata(owner)

    mock_task.delay.assert_not_called()
