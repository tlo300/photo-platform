"""Unit tests for Live Photo HEIC+MP4 auto-pairing during Takeout zip import (#106).

Tests cover:
  1. _build_live_photo_pairs — correctly identifies still+video pairs
  2. _build_live_photo_pairs — ignores entries with only a still or only a video
  3. _ingest_one — skips companion video files (they are paired to a still)
  4. _ingest_one — processes the still with live_video_obj supplied to storage
  5. _ingest_one — standalone still (no companion) is ingested without live video
  6. End-to-end: create_asset called twice total, MP4 never triggers a direct call

All tests are pure unit tests — no database, no object storage, no network.
Heavy imports from app.worker.takeout_tasks are mocked where they touch
infrastructure.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.worker.takeout_tasks import _build_live_photo_pairs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_zip(entries: dict[str, bytes | str]) -> zipfile.ZipFile:
    """Return an in-memory ZipFile containing *entries*."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            if isinstance(content, str):
                content = content.encode("utf-8")
            zf.writestr(name, content)
    buf.seek(0)
    return zipfile.ZipFile(buf, "r")


def _sidecar_json(photo_taken_ts: int = 1672531200) -> str:
    return json.dumps({"photoTakenTime": {"timestamp": str(photo_taken_ts)}})


# Minimal fake HEIC magic bytes — just enough that filetype.guess won't reject it
# outright (we mock _mime_from_magic anyway so the actual bytes don't matter).
_FAKE_HEIC = b"\x00\x00\x00\x18ftyp" + b"\x00" * 100
_FAKE_MP4 = b"\x00\x00\x00\x18ftyp" + b"mp41" + b"\x00" * 100
_FAKE_JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 100


# ---------------------------------------------------------------------------
# _build_live_photo_pairs — pure logic tests (no DB, no storage)
# ---------------------------------------------------------------------------


def test_build_pairs_detects_heic_mp4():
    zf = _make_zip(
        {
            "Photos from 2023/IMG_1234.HEIC": _FAKE_HEIC,
            "Photos from 2023/IMG_1234.MP4": _FAKE_MP4,
            "Photos from 2023/IMG_1234.HEIC.json": _sidecar_json(),
        }
    )
    pairs = _build_live_photo_pairs(zf.namelist())
    key = ("Photos from 2023", "img_1234")
    assert key in pairs
    sides = pairs[key]
    assert sides["still"] == "Photos from 2023/IMG_1234.HEIC"
    assert sides["video"] == "Photos from 2023/IMG_1234.MP4"


def test_build_pairs_detects_jpg_mov():
    zf = _make_zip(
        {
            "2022/photo.jpg": _FAKE_JPG,
            "2022/photo.MOV": _FAKE_MP4,
        }
    )
    pairs = _build_live_photo_pairs(zf.namelist())
    key = ("2022", "photo")
    assert key in pairs
    assert "still" in pairs[key]
    assert "video" in pairs[key]


def test_build_pairs_ignores_standalone_still():
    zf = _make_zip({"Photos from 2023/standalone.jpg": _FAKE_JPG})
    pairs = _build_live_photo_pairs(zf.namelist())
    assert ("Photos from 2023", "standalone") not in pairs


def test_build_pairs_ignores_standalone_video():
    zf = _make_zip({"Photos from 2023/clip.mp4": _FAKE_MP4})
    pairs = _build_live_photo_pairs(zf.namelist())
    assert ("Photos from 2023", "clip") not in pairs


def test_build_pairs_case_insensitive_stem():
    """IMG_1234.HEIC and IMG_1234.mp4 (mixed case) should still pair."""
    zf = _make_zip(
        {
            "dir/IMG_1234.HEIC": _FAKE_HEIC,
            "dir/IMG_1234.mp4": _FAKE_MP4,
        }
    )
    pairs = _build_live_photo_pairs(zf.namelist())
    assert ("dir", "img_1234") in pairs


def test_build_pairs_different_dirs_not_paired():
    """A still in dir/a and video in dir/b must NOT be paired."""
    zf = _make_zip(
        {
            "dir/a/photo.heic": _FAKE_HEIC,
            "dir/b/photo.mp4": _FAKE_MP4,
        }
    )
    pairs = _build_live_photo_pairs(zf.namelist())
    # Neither key should have both sides
    for sides in pairs.values():
        assert not ("still" in sides and "video" in sides)


# ---------------------------------------------------------------------------
# _ingest_one integration — mocked infrastructure
# ---------------------------------------------------------------------------
#
# We test _ingest_one directly by:
#   • Building an in-memory zip
#   • Mocking storage_service (upload + upload_live_video)
#   • Mocking the DB session (AsyncMock)
#   • Mocking heavy helpers (extract_exif, apply_exif, apply_sidecar,
#     generate_thumbnails, _mime_from_magic, _sha256_bytes) to keep tests fast
#   • Asserting call counts and argument patterns
#
# We do NOT test MediaService.create_asset here — _ingest_one bypasses it
# and calls storage_service directly.


def _make_mock_session():
    """Return a minimal async session mock that supports begin_nested / flush."""
    session = AsyncMock()
    # scalar() returns None → no duplicate found
    session.scalar = AsyncMock(return_value=None)
    session.flush = AsyncMock()
    session.execute = AsyncMock()

    # begin_nested() must be an async context manager
    savepoint_cm = AsyncMock()
    savepoint_cm.__aenter__ = AsyncMock(return_value=None)
    savepoint_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=savepoint_cm)
    session.add = MagicMock()
    return session


@pytest.mark.asyncio
async def test_ingest_one_live_photo_uploads_video():
    """Still with a paired companion video → upload_live_video is called, is_live_photo set."""
    import app.worker.takeout_tasks as _mod

    zf = _make_zip(
        {
            "Photos from 2023/IMG_1234.HEIC": _FAKE_HEIC,
            "Photos from 2023/IMG_1234.MP4": _FAKE_MP4,
            "Photos from 2023/IMG_1234.HEIC.json": _sidecar_json(),
        }
    )

    live_photo_pairs = _build_live_photo_pairs(zf.namelist())
    paired_video_names = {sides["video"] for sides in live_photo_pairs.values()}

    mock_storage = MagicMock()
    mock_storage.upload = MagicMock(return_value="fake/key/original.heic")
    mock_storage.upload_live_video = MagicMock(return_value="fake/key/live.mp4")
    mock_storage.delete = MagicMock()

    mock_job = MagicMock()
    mock_job.errors = []
    mock_job.duplicates = 0
    mock_job.no_sidecar = 0

    fake_asset_id = uuid.uuid4()
    fake_owner_id = uuid.uuid4()

    session = _make_mock_session()

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
        patch("uuid.uuid4", return_value=fake_asset_id),
    ):
        mock_thumb.delay = MagicMock()
        await _mod._ingest_one(
            session,
            mock_job,
            fake_owner_id,
            zf,
            "Photos from 2023/IMG_1234.HEIC",
            sidecar_map={},
            album_index=None,
            live_photo_pairs=live_photo_pairs,
            paired_video_names=paired_video_names,
        )

    # upload called once for the still
    mock_storage.upload.assert_called_once()
    # upload_live_video called once for the MP4 companion
    mock_storage.upload_live_video.assert_called_once()
    call_args = mock_storage.upload_live_video.call_args
    assert call_args.args[2] is not None  # file_obj supplied
    assert call_args.args[3] == ".mp4"   # suffix
    assert call_args.args[4] == "video/mp4"  # mime

    # The asset added to the session must have is_live_photo=True
    added_assets = [
        call.args[0]
        for call in session.add.call_args_list
        if hasattr(call.args[0], "is_live_photo")
    ]
    assert any(getattr(a, "is_live_photo", False) for a in added_assets)


@pytest.mark.asyncio
async def test_ingest_one_companion_video_is_skipped():
    """When media_name is the companion video in a pair, _ingest_one returns immediately."""
    import app.worker.takeout_tasks as _mod

    zf = _make_zip(
        {
            "Photos from 2023/IMG_1234.HEIC": _FAKE_HEIC,
            "Photos from 2023/IMG_1234.MP4": _FAKE_MP4,
        }
    )

    live_photo_pairs = _build_live_photo_pairs(zf.namelist())
    paired_video_names = {sides["video"] for sides in live_photo_pairs.values()}

    mock_storage = MagicMock()
    mock_job = MagicMock()
    mock_job.errors = []
    session = _make_mock_session()

    with patch.object(_mod, "storage_service", mock_storage):
        await _mod._ingest_one(
            session,
            mock_job,
            uuid.uuid4(),
            zf,
            "Photos from 2023/IMG_1234.MP4",
            sidecar_map={},
            album_index=None,
            live_photo_pairs=live_photo_pairs,
            paired_video_names=paired_video_names,
        )

    # Nothing uploaded — companion was skipped
    mock_storage.upload.assert_not_called()
    mock_storage.upload_live_video.assert_not_called()
    session.scalar.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_one_standalone_still_no_live_video():
    """A still with no companion video is ingested normally without calling upload_live_video."""
    import app.worker.takeout_tasks as _mod

    zf = _make_zip(
        {
            "Photos from 2023/IMG_1234.HEIC": _FAKE_HEIC,
            "Photos from 2023/IMG_1234.MP4": _FAKE_MP4,
            "Photos from 2023/standalone.jpg": _FAKE_JPG,
        }
    )

    live_photo_pairs = _build_live_photo_pairs(zf.namelist())
    paired_video_names = {sides["video"] for sides in live_photo_pairs.values()}

    mock_storage = MagicMock()
    mock_storage.upload = MagicMock(return_value="fake/key/original.jpg")
    mock_storage.upload_live_video = MagicMock()

    mock_job = MagicMock()
    mock_job.errors = []
    mock_job.duplicates = 0
    mock_job.no_sidecar = 0

    session = _make_mock_session()

    with (
        patch.object(_mod, "storage_service", mock_storage),
        patch.object(_mod, "_mime_from_magic", return_value="image/jpeg"),
        patch.object(_mod, "_sha256_bytes", return_value="cafebabe"),
        patch.object(_mod, "extract_exif", return_value=MagicMock(captured_at=None)),
        patch.object(_mod, "apply_exif", new_callable=lambda: lambda: AsyncMock()),
        patch.object(_mod, "apply_sidecar", new_callable=lambda: lambda: AsyncMock()),
        patch.object(_mod, "_read_sidecar", return_value=None),
        patch.object(_mod, "parse_sidecar", return_value=None),
        patch.object(_mod, "merge_metadata", return_value=MagicMock(captured_at=None)),
        patch("app.worker.thumbnail_tasks.generate_thumbnails") as mock_thumb,
        patch("uuid.uuid4", return_value=uuid.uuid4()),
    ):
        mock_thumb.delay = MagicMock()
        await _mod._ingest_one(
            session,
            mock_job,
            uuid.uuid4(),
            zf,
            "Photos from 2023/standalone.jpg",
            sidecar_map={},
            album_index=None,
            live_photo_pairs=live_photo_pairs,
            paired_video_names=paired_video_names,
        )

    mock_storage.upload.assert_called_once()
    mock_storage.upload_live_video.assert_not_called()


# ---------------------------------------------------------------------------
# End-to-end pairing flow: two create calls, MP4 never standalone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_zip_pairing_call_counts():
    """Simulate _process loop manually: 2 create calls, 0 direct MP4 calls."""
    import app.worker.takeout_tasks as _mod

    zf = _make_zip(
        {
            "Photos from 2023/IMG_1234.HEIC": _FAKE_HEIC,
            "Photos from 2023/IMG_1234.MP4": _FAKE_MP4,
            "Photos from 2023/IMG_1234.HEIC.json": _sidecar_json(),
            "Photos from 2023/standalone.jpg": _FAKE_JPG,
        }
    )

    all_names = zf.namelist()
    media_names = [n for n in all_names if _mod._is_media_entry(n)]
    sidecar_map = {n.lower(): n for n in all_names if n.lower().endswith(_mod._SIDECAR_EXT)}
    live_photo_pairs = _build_live_photo_pairs(all_names)
    paired_video_names = {sides["video"] for sides in live_photo_pairs.values()}

    upload_calls: list[str] = []  # track each still upload by media_name suffix

    def _fake_upload(owner_id, asset_id, file_obj, suffix, mime):
        upload_calls.append(("still", suffix))
        return f"fake/{asset_id}/original{suffix}"

    def _fake_upload_live(owner_id, asset_id, file_obj, suffix, mime):
        upload_calls.append(("live", suffix))
        return f"fake/{asset_id}/live{suffix}"

    mock_storage = MagicMock()
    mock_storage.upload = MagicMock(side_effect=_fake_upload)
    mock_storage.upload_live_video = MagicMock(side_effect=_fake_upload_live)

    mock_job = MagicMock()
    mock_job.errors = []
    mock_job.duplicates = 0
    mock_job.no_sidecar = 0

    session = _make_mock_session()

    with (
        patch.object(_mod, "storage_service", mock_storage),
        patch.object(_mod, "_mime_from_magic", side_effect=lambda d: (
            "image/heic" if d[:8] == _FAKE_HEIC[:8] else "image/jpeg"
        )),
        patch.object(_mod, "_sha256_bytes", side_effect=lambda d: d[:8].hex()),
        patch.object(_mod, "extract_exif", return_value=MagicMock(captured_at=None)),
        patch.object(_mod, "apply_exif", new_callable=lambda: lambda: AsyncMock()),
        patch.object(_mod, "apply_sidecar", new_callable=lambda: lambda: AsyncMock()),
        patch.object(_mod, "_read_sidecar", return_value=None),
        patch.object(_mod, "parse_sidecar", return_value=None),
        patch.object(_mod, "merge_metadata", return_value=MagicMock(captured_at=None)),
        patch("app.worker.thumbnail_tasks.generate_thumbnails") as mock_thumb,
    ):
        mock_thumb.delay = MagicMock()
        for media_name in media_names:
            await _mod._ingest_one(
                session,
                mock_job,
                uuid.uuid4(),
                zf,
                media_name,
                sidecar_map=sidecar_map,
                album_index=None,
                live_photo_pairs=live_photo_pairs,
                paired_video_names=paired_video_names,
            )

    # Exactly two still uploads (HEIC + standalone.jpg), one live video upload
    still_uploads = [c for c in upload_calls if c[0] == "still"]
    live_uploads = [c for c in upload_calls if c[0] == "live"]
    assert len(still_uploads) == 2, f"Expected 2 still uploads, got {still_uploads}"
    assert len(live_uploads) == 1, f"Expected 1 live upload, got {live_uploads}"
    assert live_uploads[0][1] == ".mp4"
    # No direct standalone upload for .mp4
    mp4_stills = [c for c in still_uploads if c[1] == ".mp4"]
    assert mp4_stills == [], "MP4 should never be uploaded as a standalone still"
