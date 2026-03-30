"""Unit tests for Live Photo pair JSON storage (#134).

Covers the three paths that create a Live Photo pair:
  1. Direct upload (_ingest_one in upload_tasks)
  2. Takeout zip import (_ingest_one in takeout_tasks)
  3. Backfill (_run_pair_backfill in metadata_tasks)

All tests are pure unit tests — no database, no real object storage, no network.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

_FAKE_HEIC = b"\x00\x00\x00\x18ftyp" + b"\x00" * 100
_FAKE_MP4 = b"\x00\x00\x00\x18ftyp" + b"mp41" + b"\x00" * 100


def _make_zip(entries: dict[str, bytes]) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    buf.seek(0)
    return zipfile.ZipFile(buf, "r")


def _make_upload_session():
    """Minimal async session mock for upload_tasks._ingest_one."""
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    session.execute = AsyncMock()
    savepoint_cm = AsyncMock()
    savepoint_cm.__aenter__ = AsyncMock(return_value=None)
    savepoint_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=savepoint_cm)
    session.add = MagicMock()
    return session


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
    asset.storage_key = storage_key or f"{asset.owner_id}/{asset.id}/original.heic"
    asset.is_live_photo = is_live_photo
    asset.live_video_key = live_video_key
    return asset


def _scalars_result(items: list):
    result = MagicMock()
    result.__iter__ = MagicMock(return_value=iter(items))
    return result


def _make_backfill_session(*, photos: list, videos: list) -> AsyncMock:
    """Minimal async session mock for _run_pair_backfill."""
    session = AsyncMock()
    session.scalars = AsyncMock(side_effect=[
        _scalars_result(photos),
        _scalars_result(videos),
    ])
    session.delete = AsyncMock()
    session.commit = AsyncMock()
    return session


def _patch_backfill_session(session: AsyncMock):
    """Context-manager factories that inject *session* into _run_pair_backfill."""
    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    session_maker = MagicMock(return_value=session_cm)
    engine = MagicMock()
    engine.dispose = AsyncMock()
    return (
        patch("app.worker.metadata_tasks.create_async_engine", return_value=engine),
        patch("app.worker.metadata_tasks.async_sessionmaker", return_value=session_maker),
    )


# ---------------------------------------------------------------------------
# 1. Direct upload path (upload_tasks._ingest_one)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_upload_writes_pair_json():
    """_ingest_one in upload_tasks calls upload_pair_json for a live pair."""
    import app.worker.upload_tasks as _mod

    owner_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    mock_storage = MagicMock()
    mock_storage.upload = MagicMock(return_value=f"{owner_id}/{asset_id}/original.heic")
    mock_storage.upload_live_video = MagicMock(return_value=f"{owner_id}/{asset_id}/live.mp4")
    mock_storage.upload_pair_json = MagicMock(return_value=f"{owner_id}/{asset_id}/pair.json")

    mock_job = MagicMock()
    mock_job.errors = []
    mock_job.duplicates = 0
    mock_job.no_sidecar = 0

    session = _make_upload_session()

    with (
        patch.object(_mod, "storage_service", mock_storage),
        patch.object(_mod, "_detect_mime", return_value="image/heic"),
        patch.object(_mod, "_sha256", return_value="deadbeef"),
        patch.object(_mod, "extract_exif", return_value=MagicMock(
            captured_at=None,
            gps_latitude=None,
            gps_longitude=None,
            gps_altitude=None,
        )),
        patch.object(_mod, "apply_exif", new=AsyncMock()),
        patch.object(_mod, "merge_metadata", return_value=MagicMock(captured_at=None)),
        patch("app.worker.thumbnail_tasks.generate_thumbnails") as mock_thumb,
        patch("uuid.uuid4", return_value=asset_id),
    ):
        mock_thumb.delay = MagicMock()
        await _mod._ingest_one(
            session,
            mock_job,
            owner_id,
            _FAKE_HEIC,
            "IMG_001.heic",
            rel_path="IMG_001.heic",
            target_album_id=None,
            parsed_sidecar=None,
            live_video_data=_FAKE_MP4,
            live_video_filename="IMG_001.mp4",
        )

    mock_storage.upload_pair_json.assert_called_once()
    call_kwargs = mock_storage.upload_pair_json.call_args
    assert call_kwargs.args[0] == str(owner_id)
    assert call_kwargs.args[1] == str(asset_id)
    payload = call_kwargs.args[2]
    assert payload["version"] == 1
    assert payload["asset_id"] == str(asset_id)
    assert payload["still_filename"] == "IMG_001.heic"
    assert payload["video_filename"] == "IMG_001.mp4"
    assert "still_key" in payload
    assert "video_key" in payload
    assert mock_job.errors == []


@pytest.mark.asyncio
async def test_direct_upload_no_pair_json_for_standalone():
    """_ingest_one in upload_tasks does NOT call upload_pair_json for a non-live asset."""
    import app.worker.upload_tasks as _mod

    owner_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    mock_storage = MagicMock()
    mock_storage.upload = MagicMock(return_value=f"{owner_id}/{asset_id}/original.jpg")
    mock_storage.upload_pair_json = MagicMock()

    mock_job = MagicMock()
    mock_job.errors = []
    mock_job.duplicates = 0
    mock_job.no_sidecar = 0

    session = _make_upload_session()

    with (
        patch.object(_mod, "storage_service", mock_storage),
        patch.object(_mod, "_detect_mime", return_value="image/jpeg"),
        patch.object(_mod, "_sha256", return_value="cafebabe"),
        patch.object(_mod, "extract_exif", return_value=MagicMock(
            captured_at=None,
            gps_latitude=None,
            gps_longitude=None,
            gps_altitude=None,
        )),
        patch.object(_mod, "apply_exif", new=AsyncMock()),
        patch.object(_mod, "merge_metadata", return_value=MagicMock(captured_at=None)),
        patch("app.worker.thumbnail_tasks.generate_thumbnails") as mock_thumb,
        patch("uuid.uuid4", return_value=asset_id),
    ):
        mock_thumb.delay = MagicMock()
        await _mod._ingest_one(
            session,
            mock_job,
            owner_id,
            _FAKE_HEIC,
            "standalone.jpg",
            rel_path="standalone.jpg",
            target_album_id=None,
        )

    mock_storage.upload_pair_json.assert_not_called()
    assert mock_job.errors == []


@pytest.mark.asyncio
async def test_direct_upload_pair_json_failure_does_not_abort():
    """A StorageError from upload_pair_json is swallowed — ingest succeeds."""
    import app.worker.upload_tasks as _mod
    from app.services.storage import StorageError

    owner_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    mock_storage = MagicMock()
    mock_storage.upload = MagicMock(return_value=f"{owner_id}/{asset_id}/original.heic")
    mock_storage.upload_live_video = MagicMock(return_value=f"{owner_id}/{asset_id}/live.mp4")
    mock_storage.upload_pair_json = MagicMock(side_effect=StorageError("network hiccup"))

    mock_job = MagicMock()
    mock_job.errors = []
    mock_job.duplicates = 0
    mock_job.no_sidecar = 0

    session = _make_upload_session()

    with (
        patch.object(_mod, "storage_service", mock_storage),
        patch.object(_mod, "_detect_mime", return_value="image/heic"),
        patch.object(_mod, "_sha256", return_value="deadbeef"),
        patch.object(_mod, "extract_exif", return_value=MagicMock(
            captured_at=None,
            gps_latitude=None,
            gps_longitude=None,
            gps_altitude=None,
        )),
        patch.object(_mod, "apply_exif", new=AsyncMock()),
        patch.object(_mod, "merge_metadata", return_value=MagicMock(captured_at=None)),
        patch("app.worker.thumbnail_tasks.generate_thumbnails") as mock_thumb,
        patch("uuid.uuid4", return_value=asset_id),
    ):
        mock_thumb.delay = MagicMock()
        await _mod._ingest_one(
            session,
            mock_job,
            owner_id,
            _FAKE_HEIC,
            "IMG_001.heic",
            rel_path="IMG_001.heic",
            target_album_id=None,
            live_video_data=_FAKE_MP4,
            live_video_filename="IMG_001.mp4",
        )

    mock_storage.upload_pair_json.assert_called_once()
    assert mock_job.errors == []


# ---------------------------------------------------------------------------
# 2. Takeout zip import path (takeout_tasks._ingest_one)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_takeout_ingest_writes_pair_json():
    """_ingest_one in takeout_tasks calls upload_pair_json for a live pair."""
    import app.worker.takeout_tasks as _mod
    from app.worker.takeout_tasks import _build_live_photo_pairs

    zf = _make_zip(
        {
            "Photos from 2023/IMG_1234.HEIC": _FAKE_HEIC,
            "Photos from 2023/IMG_1234.MP4": _FAKE_MP4,
        }
    )

    live_photo_pairs = _build_live_photo_pairs(zf.namelist())
    paired_video_names = {sides["video"] for sides in live_photo_pairs.values()}

    owner_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    mock_storage = MagicMock()
    mock_storage.upload = MagicMock(return_value=f"{owner_id}/{asset_id}/original.heic")
    mock_storage.upload_live_video = MagicMock(return_value=f"{owner_id}/{asset_id}/live.mp4")
    mock_storage.upload_pair_json = MagicMock(return_value=f"{owner_id}/{asset_id}/pair.json")

    mock_job = MagicMock()
    mock_job.errors = []
    mock_job.duplicates = 0
    mock_job.no_sidecar = 0

    session = _make_upload_session()

    with (
        patch.object(_mod, "storage_service", mock_storage),
        patch.object(_mod, "_mime_from_magic", return_value="image/heic"),
        patch.object(_mod, "_sha256_bytes", return_value="deadbeef"),
        patch.object(_mod, "extract_exif", return_value=MagicMock(captured_at=None)),
        patch.object(_mod, "apply_exif", new_callable=lambda: lambda: AsyncMock()),
        patch.object(_mod, "apply_sidecar", new_callable=lambda: lambda: AsyncMock()),
        patch.object(_mod, "_read_sidecar", return_value=None),
        patch.object(_mod, "parse_sidecar", return_value=None),
        patch.object(_mod, "merge_metadata", return_value=MagicMock(captured_at=None)),
        patch("app.worker.thumbnail_tasks.generate_thumbnails") as mock_thumb,
        patch("uuid.uuid4", return_value=asset_id),
    ):
        mock_thumb.delay = MagicMock()
        await _mod._ingest_one(
            session,
            mock_job,
            owner_id,
            zf,
            "Photos from 2023/IMG_1234.HEIC",
            sidecar_map={},
            album_index=None,
            live_photo_pairs=live_photo_pairs,
            paired_video_names=paired_video_names,
        )

    mock_storage.upload_pair_json.assert_called_once()
    payload = mock_storage.upload_pair_json.call_args.args[2]
    assert payload["version"] == 1
    assert payload["still_filename"] == "IMG_1234.HEIC"
    assert payload["video_filename"] == "IMG_1234.MP4"
    assert payload["still_key"] == f"{owner_id}/{asset_id}/original.heic"
    assert payload["video_key"] == f"{owner_id}/{asset_id}/live.mp4"


# ---------------------------------------------------------------------------
# 3. Backfill path (metadata_tasks._run_pair_backfill)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_writes_pair_json():
    """_run_pair_backfill calls upload_pair_json for each successfully merged pair."""
    from app.worker.metadata_tasks import _run_pair_backfill

    owner_id = uuid.uuid4()
    photo_id = uuid.uuid4()
    video_id = uuid.uuid4()

    photo = _make_asset(
        id=photo_id,
        owner_id=owner_id,
        mime_type="image/heic",
        original_filename="Photos from 2023/IMG_001.HEIC",
        storage_key=f"{owner_id}/{photo_id}/original.heic",
    )
    video = _make_asset(
        id=video_id,
        owner_id=owner_id,
        mime_type="video/mp4",
        original_filename="Photos from 2023/IMG_001.MP4",
        storage_key=f"{owner_id}/{video_id}/original.mp4",
    )

    session = _make_backfill_session(photos=[photo], videos=[video])
    p1, p2 = _patch_backfill_session(session)

    mock_storage = MagicMock()
    mock_storage._client = MagicMock()
    mock_storage._bucket = "photos"
    mock_storage.upload_pair_json = MagicMock(
        return_value=f"{owner_id}/{photo_id}/pair.json"
    )

    with p1, p2, patch("app.worker.metadata_tasks.storage_service", mock_storage):
        await _run_pair_backfill(owner_id)

    mock_storage.upload_pair_json.assert_called_once()
    call_args = mock_storage.upload_pair_json.call_args
    assert call_args.args[0] == str(owner_id)
    assert call_args.args[1] == str(photo_id)
    payload = call_args.args[2]
    assert payload["version"] == 1
    assert payload["asset_id"] == str(photo_id)
    assert payload["still_filename"] == photo.original_filename
    assert payload["video_filename"] == video.original_filename
    assert payload["still_key"] == photo.storage_key
    assert payload["video_key"].endswith(".mp4")
    assert "live" in payload["video_key"]
