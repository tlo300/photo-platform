"""Unit tests for Google Takeout sidecar filename matching logic.

No database, no object storage — pure logic tests for the helper functions
in app.worker.takeout_tasks.
"""

import pytest

from app.worker.takeout_tasks import _is_media_entry, _sidecar_name


# ---------------------------------------------------------------------------
# _sidecar_name
# ---------------------------------------------------------------------------


def test_sidecar_name_simple_jpeg():
    assert _sidecar_name("photo.jpg") == "photo.jpg.json"


def test_sidecar_name_nested_path():
    assert _sidecar_name("Photos/2023/image.png") == "Photos/2023/image.png.json"


def test_sidecar_name_video():
    assert _sidecar_name("clip.mp4") == "clip.mp4.json"


def test_sidecar_name_short_stem_not_truncated():
    # Stem is 5 chars, well under 46 — no truncation
    assert _sidecar_name("short.jpg") == "short.jpg.json"


def test_sidecar_name_stem_at_limit_not_truncated():
    # Stem is exactly 46 chars — no truncation
    stem = "a" * 46
    assert _sidecar_name(f"{stem}.jpg") == f"{stem}.jpg.json"


def test_sidecar_name_stem_over_limit_truncated():
    # Stem is 47 chars — truncated to 46
    stem = "b" * 47
    expected_stem = "b" * 46
    assert _sidecar_name(f"{stem}.jpg") == f"{expected_stem}.jpg.json"


def test_sidecar_name_no_extension():
    # File with no extension — stem is the whole name
    assert _sidecar_name("README") == "README.json"


# ---------------------------------------------------------------------------
# _is_media_entry
# ---------------------------------------------------------------------------


def test_is_media_entry_accepts_jpeg():
    assert _is_media_entry("photos/image.jpg") is True


def test_is_media_entry_accepts_mp4():
    assert _is_media_entry("videos/clip.mp4") is True


def test_is_media_entry_rejects_json_sidecar():
    assert _is_media_entry("photos/image.jpg.json") is False


def test_is_media_entry_rejects_directory_entry():
    assert _is_media_entry("photos/subfolder/") is False


def test_is_media_entry_rejects_macos_metadata():
    assert _is_media_entry("__MACOSX/photos/._image.jpg") is False


def test_is_media_entry_rejects_macos_metadata_mixed_case():
    assert _is_media_entry("__MACOSX/Photos/._IMG_0001.HEIC") is False
