"""Unit tests for the thumbnail generation Celery task (issue #23).

All external I/O (MinIO, DB, ffmpeg subprocess) is mocked.
No live services are required.
"""

from __future__ import annotations

import io
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

# ---------------------------------------------------------------------------
# Helpers to build minimal test images
# ---------------------------------------------------------------------------


def _make_jpeg_bytes(width: int = 400, height: int = 300) -> bytes:
    img = Image.new("RGB", (width, height), color=(100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _make_png_bytes() -> bytes:
    img = Image.new("RGB", (200, 200), color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Happy path — image asset
# ---------------------------------------------------------------------------


def test_generate_thumbnails_image_happy_path():
    """Task generates thumb + preview and marks thumbnail_ready=true."""
    asset_id = str(uuid.uuid4())
    owner_id = str(uuid.uuid4())
    jpeg_data = _make_jpeg_bytes()
    storage_key = f"{owner_id}/{asset_id}/original.jpg"

    with (
        patch(
            "app.worker.thumbnail_tasks._get_asset",
            new=AsyncMock(return_value=(storage_key, "image/jpeg")),
        ),
        patch(
            "app.worker.thumbnail_tasks._set_thumbnail_ready",
            new=AsyncMock(),
        ) as mock_ready,
        patch(
            "app.worker.thumbnail_tasks._set_thumbnail_error",
            new=AsyncMock(),
        ) as mock_error,
        patch("app.worker.thumbnail_tasks.storage_service") as mock_storage,
    ):
        # Simulate get_object returning JPEG bytes
        mock_storage._client.get_object.return_value = {
            "Body": io.BytesIO(jpeg_data)
        }

        from app.worker.thumbnail_tasks import generate_thumbnails

        generate_thumbnails(asset_id, owner_id)

    # Both thumbnails uploaded
    assert mock_storage._client.put_object.call_count == 2
    put_keys = {
        call.kwargs["Key"]
        for call in mock_storage._client.put_object.call_args_list
    }
    assert f"{owner_id}/thumbnails/{asset_id}/thumb.webp" in put_keys
    assert f"{owner_id}/thumbnails/{asset_id}/preview.webp" in put_keys

    # Thumbnail ready set, no error
    mock_ready.assert_awaited_once()
    mock_error.assert_not_awaited()


# ---------------------------------------------------------------------------
# Happy path — video asset (ffmpeg first-frame)
# ---------------------------------------------------------------------------


def test_generate_thumbnails_video_happy_path():
    """Task uses ffmpeg for video and marks thumbnail_ready=true."""
    asset_id = str(uuid.uuid4())
    owner_id = str(uuid.uuid4())
    png_data = _make_png_bytes()  # ffmpeg "extracts" this as the first frame
    storage_key = f"{owner_id}/{asset_id}/original.mp4"

    def fake_subprocess_run(cmd, *, check, capture_output):
        # Write a valid PNG to the frame_path argument (last arg in the ffmpeg call)
        frame_path = cmd[-1]
        with open(frame_path, "wb") as f:
            f.write(png_data)
        result = MagicMock()
        result.returncode = 0
        return result

    with (
        patch(
            "app.worker.thumbnail_tasks._get_asset",
            new=AsyncMock(return_value=(storage_key, "video/mp4")),
        ),
        patch(
            "app.worker.thumbnail_tasks._set_thumbnail_ready",
            new=AsyncMock(),
        ) as mock_ready,
        patch(
            "app.worker.thumbnail_tasks._set_thumbnail_error",
            new=AsyncMock(),
        ) as mock_error,
        patch("app.worker.thumbnail_tasks.storage_service") as mock_storage,
        patch("app.worker.thumbnail_tasks.subprocess.run", side_effect=fake_subprocess_run),
    ):
        mock_storage._client.get_object.return_value = {
            "Body": io.BytesIO(b"\x00" * 16)  # fake video bytes — ffmpeg is mocked
        }

        from app.worker.thumbnail_tasks import generate_thumbnails

        generate_thumbnails(asset_id, owner_id)

    assert mock_storage._client.put_object.call_count == 2
    mock_ready.assert_awaited_once()
    mock_error.assert_not_awaited()


# ---------------------------------------------------------------------------
# Error path — retries exhausted → thumbnail_error set
# ---------------------------------------------------------------------------


def test_generate_thumbnails_sets_error_after_max_retries():
    """After all retries are exhausted, thumbnail_error is set to true."""
    from celery.exceptions import MaxRetriesExceededError

    asset_id = str(uuid.uuid4())
    owner_id = str(uuid.uuid4())
    storage_key = f"{owner_id}/{asset_id}/original.jpg"

    from app.worker.thumbnail_tasks import generate_thumbnails

    with (
        patch(
            "app.worker.thumbnail_tasks._get_asset",
            new=AsyncMock(return_value=(storage_key, "image/jpeg")),
        ),
        patch(
            "app.worker.thumbnail_tasks._set_thumbnail_ready",
            new=AsyncMock(),
        ) as mock_ready,
        patch(
            "app.worker.thumbnail_tasks._set_thumbnail_error",
            new=AsyncMock(),
        ) as mock_error,
        patch("app.worker.thumbnail_tasks.storage_service") as mock_storage,
        # Make retry() raise MaxRetriesExceededError (simulates exhausted retries)
        patch.object(generate_thumbnails, "retry", side_effect=MaxRetriesExceededError()),
    ):
        mock_storage._client.get_object.side_effect = Exception("connection refused")

        # apply() runs the task synchronously; EAGER mode bypasses the broker
        generate_thumbnails.apply(args=[asset_id, owner_id])

    mock_error.assert_awaited_once()
    mock_ready.assert_not_awaited()


# ---------------------------------------------------------------------------
# Edge case — asset not found in DB
# ---------------------------------------------------------------------------


def test_generate_thumbnails_skips_missing_asset():
    """Task exits cleanly when the asset row no longer exists."""
    asset_id = str(uuid.uuid4())
    owner_id = str(uuid.uuid4())

    with (
        patch(
            "app.worker.thumbnail_tasks._get_asset",
            new=AsyncMock(return_value=None),
        ),
        patch("app.worker.thumbnail_tasks.storage_service") as mock_storage,
        patch(
            "app.worker.thumbnail_tasks._set_thumbnail_ready",
            new=AsyncMock(),
        ) as mock_ready,
        patch(
            "app.worker.thumbnail_tasks._set_thumbnail_error",
            new=AsyncMock(),
        ) as mock_error,
    ):
        from app.worker.thumbnail_tasks import generate_thumbnails

        generate_thumbnails(asset_id, owner_id)

    mock_storage._client.get_object.assert_not_called()
    mock_ready.assert_not_awaited()
    mock_error.assert_not_awaited()
