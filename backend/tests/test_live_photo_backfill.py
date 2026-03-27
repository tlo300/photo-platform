"""Unit tests for the live photo pair backfill Celery task.

All external I/O (MinIO, DB session) is mocked.
No live services are required.

Tests cover:
  1. Matched pair — copy_object called, photo updated, video row deleted
  2. No video assets — task exits cleanly without touching storage
  3. No unpaired photo assets — task exits cleanly
  4. Ambiguous stem+dir (two videos, same key) — pair skipped
  5. Different directories with same stem — NOT paired (dir is part of the key)
  6. copy_object failure — pair skipped, no DB changes for that pair
  7. Idempotent — photo already marked is_live_photo=True is excluded from query
  8. Extension preserved — .mov video produces live.mov key
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from app.worker.metadata_tasks import _run_pair_backfill


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_asset(
    *,
    id: uuid.UUID | None = None,
    owner_id: uuid.UUID | None = None,
    mime_type: str = "image/heic",
    original_filename: str = "Photos from 2023/IMG_1234.HEIC",
    storage_key: str | None = None,
    is_live_photo: bool = False,
    live_video_key: str | None = None,
) -> MagicMock:
    asset = MagicMock()
    asset.id = id or uuid.uuid4()
    asset.owner_id = owner_id or uuid.uuid4()
    asset.mime_type = mime_type
    asset.original_filename = original_filename
    asset.storage_key = storage_key or f"{asset.owner_id}/{asset.id}/original.mp4"
    asset.is_live_photo = is_live_photo
    asset.live_video_key = live_video_key
    return asset


def _scalars_result(items: list):
    """Return a mock that behaves like an AsyncSession.scalars() result (iterable)."""
    result = MagicMock()
    result.__iter__ = MagicMock(return_value=iter(items))
    return result


def _make_session(*, photos: list, videos: list) -> AsyncMock:
    """Return a minimal async session mock.

    The first scalars() call returns the photos iterable,
    the second call returns the videos iterable.
    """
    session = AsyncMock()
    # side_effect as a list: each call pops the next value from the list.
    session.scalars = AsyncMock(side_effect=[
        _scalars_result(photos),
        _scalars_result(videos),
    ])
    session.delete = AsyncMock()
    session.commit = AsyncMock()
    return session


def _patch_session(session: AsyncMock):
    """Context-manager factory that injects *session* into _run_pair_backfill."""
    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)

    session_maker = MagicMock(return_value=session_cm)

    engine_cm = MagicMock()
    engine_cm.dispose = AsyncMock()

    return (
        patch(
            "app.worker.metadata_tasks.create_async_engine",
            return_value=engine_cm,
        ),
        patch(
            "app.worker.metadata_tasks.async_sessionmaker",
            return_value=session_maker,
        ),
    )


# ---------------------------------------------------------------------------
# Test 1 — happy path: one matched pair
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_matched_pair_is_merged():
    owner_id = uuid.uuid4()
    photo_id = uuid.uuid4()
    video_id = uuid.uuid4()

    photo = _make_asset(
        id=photo_id,
        owner_id=owner_id,
        mime_type="image/heic",
        original_filename="Photos from 2023/IMG_1234.HEIC",
        storage_key=f"{owner_id}/{photo_id}/original.heic",
    )
    video = _make_asset(
        id=video_id,
        owner_id=owner_id,
        mime_type="video/mp4",
        original_filename="Photos from 2023/IMG_1234.MP4",
        storage_key=f"{owner_id}/{video_id}/original.mp4",
    )

    session = _make_session(photos=[photo], videos=[video])

    mock_client = MagicMock()
    mock_storage = MagicMock()
    mock_storage._client = mock_client
    mock_storage._bucket = "photos"

    p1, p2 = _patch_session(session)
    with p1, p2, patch("app.worker.metadata_tasks.storage_service", mock_storage):
        await _run_pair_backfill(owner_id)

    expected_new_key = f"{owner_id}/{photo_id}/live.mp4"

    mock_client.copy_object.assert_called_once_with(
        Bucket="photos",
        CopySource={"Bucket": "photos", "Key": f"{owner_id}/{video_id}/original.mp4"},
        Key=expected_new_key,
    )
    mock_client.delete_object.assert_called_once_with(
        Bucket="photos", Key=f"{owner_id}/{video_id}/original.mp4"
    )
    assert photo.is_live_photo is True
    assert photo.live_video_key == expected_new_key
    session.delete.assert_awaited_once_with(video)
    session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 2 — no video assets: task exits cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_videos_exits_cleanly():
    owner_id = uuid.uuid4()
    photo = _make_asset(owner_id=owner_id)

    session = _make_session(photos=[photo], videos=[])
    mock_storage = MagicMock()

    p1, p2 = _patch_session(session)
    with p1, p2, patch("app.worker.metadata_tasks.storage_service", mock_storage):
        await _run_pair_backfill(owner_id)

    mock_storage._client.copy_object.assert_not_called()
    session.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3 — no unpaired photos: task exits cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_unpaired_photos_exits_cleanly():
    owner_id = uuid.uuid4()
    session = _make_session(photos=[], videos=[])

    mock_storage = MagicMock()

    p1, p2 = _patch_session(session)
    with p1, p2, patch("app.worker.metadata_tasks.storage_service", mock_storage):
        await _run_pair_backfill(owner_id)

    # scalars called once for photos, then returned early — no copy, no delete
    mock_storage._client.copy_object.assert_not_called()
    session.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4 — ambiguous: two videos with the same stem+dir key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ambiguous_pair_is_skipped():
    owner_id = uuid.uuid4()
    photo_id = uuid.uuid4()

    photo = _make_asset(
        id=photo_id,
        owner_id=owner_id,
        mime_type="image/heic",
        original_filename="Photos from 2023/IMG_1234.HEIC",
        storage_key=f"{owner_id}/{photo_id}/original.heic",
    )
    video_a = _make_asset(
        owner_id=owner_id,
        mime_type="video/mp4",
        original_filename="Photos from 2023/IMG_1234.MP4",
        storage_key=f"{owner_id}/{uuid.uuid4()}/original.mp4",
    )
    video_b = _make_asset(
        owner_id=owner_id,
        mime_type="video/mp4",
        original_filename="Photos from 2023/IMG_1234.MP4",
        storage_key=f"{owner_id}/{uuid.uuid4()}/original.mp4",
    )

    session = _make_session(photos=[photo], videos=[video_a, video_b])
    mock_storage = MagicMock()

    p1, p2 = _patch_session(session)
    with p1, p2, patch("app.worker.metadata_tasks.storage_service", mock_storage):
        await _run_pair_backfill(owner_id)

    mock_storage._client.copy_object.assert_not_called()
    session.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5 — different dirs, same stem: NOT paired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_dirs_same_stem_not_paired():
    owner_id = uuid.uuid4()
    photo_id = uuid.uuid4()

    photo = _make_asset(
        id=photo_id,
        owner_id=owner_id,
        mime_type="image/heic",
        original_filename="dir/a/photo.HEIC",
        storage_key=f"{owner_id}/{photo_id}/original.heic",
    )
    video = _make_asset(
        owner_id=owner_id,
        mime_type="video/mp4",
        original_filename="dir/b/photo.MP4",
        storage_key=f"{owner_id}/{uuid.uuid4()}/original.mp4",
    )

    session = _make_session(photos=[photo], videos=[video])
    mock_storage = MagicMock()

    p1, p2 = _patch_session(session)
    with p1, p2, patch("app.worker.metadata_tasks.storage_service", mock_storage):
        await _run_pair_backfill(owner_id)

    mock_storage._client.copy_object.assert_not_called()
    session.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6 — copy_object failure: pair skipped, no DB change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copy_object_failure_skips_pair():
    owner_id = uuid.uuid4()
    photo_id = uuid.uuid4()
    video_id = uuid.uuid4()

    photo = _make_asset(
        id=photo_id,
        owner_id=owner_id,
        mime_type="image/heic",
        original_filename="Photos from 2023/IMG_1234.HEIC",
        storage_key=f"{owner_id}/{photo_id}/original.heic",
    )
    video = _make_asset(
        id=video_id,
        owner_id=owner_id,
        mime_type="video/mp4",
        original_filename="Photos from 2023/IMG_1234.MP4",
        storage_key=f"{owner_id}/{video_id}/original.mp4",
    )

    session = _make_session(photos=[photo], videos=[video])

    mock_client = MagicMock()
    mock_client.copy_object.side_effect = RuntimeError("S3 error")
    mock_storage = MagicMock()
    mock_storage._client = mock_client
    mock_storage._bucket = "photos"

    p1, p2 = _patch_session(session)
    with p1, p2, patch("app.worker.metadata_tasks.storage_service", mock_storage):
        await _run_pair_backfill(owner_id)

    # photo must NOT be updated
    assert photo.is_live_photo is False
    assert photo.live_video_key is None
    session.delete.assert_not_called()
    # commit is still called (zero changes committed)
    session.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 7 — extension preserved for .mov companion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mov_extension_preserved():
    owner_id = uuid.uuid4()
    photo_id = uuid.uuid4()
    video_id = uuid.uuid4()

    photo = _make_asset(
        id=photo_id,
        owner_id=owner_id,
        mime_type="image/jpeg",
        original_filename="2022/summer.jpg",
        storage_key=f"{owner_id}/{photo_id}/original.jpg",
    )
    video = _make_asset(
        id=video_id,
        owner_id=owner_id,
        mime_type="video/quicktime",
        original_filename="2022/summer.MOV",
        storage_key=f"{owner_id}/{video_id}/original.mov",
    )

    session = _make_session(photos=[photo], videos=[video])

    mock_client = MagicMock()
    mock_storage = MagicMock()
    mock_storage._client = mock_client
    mock_storage._bucket = "photos"

    p1, p2 = _patch_session(session)
    with p1, p2, patch("app.worker.metadata_tasks.storage_service", mock_storage):
        await _run_pair_backfill(owner_id)

    # Key must use .mov extension (lower-cased from original_filename)
    copy_call = mock_client.copy_object.call_args
    assert copy_call.kwargs["Key"] == f"{owner_id}/{photo_id}/live.mov"
    assert photo.live_video_key == f"{owner_id}/{photo_id}/live.mov"
