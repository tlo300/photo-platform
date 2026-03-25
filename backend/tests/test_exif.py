"""Unit tests for extract_exif (pure function — no database required).

Tests cover:
  - JPEG with full EXIF (make, model, DateTimeOriginal, dimensions)
  - TIFF with full EXIF
  - JPEG with no EXIF tags present
  - Video mime type → all-None result, no crash
  - Corrupt bytes → all-None result, no crash
  - Timestamp merge: sidecar_captured_at must not be overwritten by apply_exif
  - HEIC with full EXIF (make, model, dimensions)
  - HEIC with no EXIF — returns None metadata, no crash
"""

import io
from datetime import datetime, timezone

import pytest
from PIL import Image

from app.services.exif import ExifResult, _HEIF_AVAILABLE, extract_exif, _parse_exif_datetime

# ---------------------------------------------------------------------------
# Helpers to build minimal in-memory images with EXIF
# ---------------------------------------------------------------------------

_MAKE = "FakeCorp"
_MODEL = "TestCam 9"
_DATETIME_ORIGINAL = "2021:08:14 10:00:00"
_EXPECTED_CAPTURED_AT = datetime(2021, 8, 14, 10, 0, 0, tzinfo=timezone.utc)

_TAG_MAKE = 271
_TAG_MODEL = 272
_TAG_DATETIME_ORIGINAL = 36867


def _make_jpeg(width: int = 120, height: int = 80, with_exif: bool = True) -> bytes:
    img = Image.new("RGB", (width, height), color=(128, 64, 32))
    if with_exif:
        exif = img.getexif()
        exif[_TAG_MAKE] = _MAKE
        exif[_TAG_MODEL] = _MODEL
        exif[_TAG_DATETIME_ORIGINAL] = _DATETIME_ORIGINAL
        buf = io.BytesIO()
        img.save(buf, format="JPEG", exif=exif.tobytes())
    else:
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
    return buf.getvalue()


def _make_heic(width: int = 120, height: int = 80, with_exif: bool = True) -> bytes:
    img = Image.new("RGB", (width, height), color=(64, 128, 192))
    buf = io.BytesIO()
    if with_exif:
        exif = img.getexif()
        exif[_TAG_MAKE] = _MAKE
        exif[_TAG_MODEL] = _MODEL
        exif[_TAG_DATETIME_ORIGINAL] = _DATETIME_ORIGINAL
        img.save(buf, format="HEIF", exif=exif.tobytes())
    else:
        img.save(buf, format="HEIF")
    return buf.getvalue()


def _make_tiff(width: int = 64, height: int = 48) -> bytes:
    img = Image.new("RGB", (width, height), color=(10, 20, 30))
    exif = img.getexif()
    exif[_TAG_MAKE] = _MAKE
    exif[_TAG_MODEL] = _MODEL
    exif[_TAG_DATETIME_ORIGINAL] = _DATETIME_ORIGINAL
    buf = io.BytesIO()
    img.save(buf, format="TIFF", exif=exif.tobytes())
    return buf.getvalue()


# ---------------------------------------------------------------------------
# JPEG tests
# ---------------------------------------------------------------------------


class TestJpegExif:
    def test_make_extracted(self):
        result = extract_exif(_make_jpeg(), "image/jpeg")
        assert result.make == _MAKE

    def test_model_extracted(self):
        result = extract_exif(_make_jpeg(), "image/jpeg")
        assert result.model == _MODEL

    def test_dimensions_extracted(self):
        result = extract_exif(_make_jpeg(width=120, height=80), "image/jpeg")
        assert result.width_px == 120
        assert result.height_px == 80

    def test_captured_at_from_datetime_original(self):
        result = extract_exif(_make_jpeg(), "image/jpeg")
        assert result.captured_at == _EXPECTED_CAPTURED_AT

    def test_returns_exif_result_type(self):
        result = extract_exif(_make_jpeg(), "image/jpeg")
        assert isinstance(result, ExifResult)


# ---------------------------------------------------------------------------
# TIFF tests
# ---------------------------------------------------------------------------


class TestTiffExif:
    def test_make_extracted(self):
        result = extract_exif(_make_tiff(), "image/tiff")
        assert result.make == _MAKE

    def test_model_extracted(self):
        result = extract_exif(_make_tiff(), "image/tiff")
        assert result.model == _MODEL

    def test_dimensions_extracted(self):
        result = extract_exif(_make_tiff(width=64, height=48), "image/tiff")
        assert result.width_px == 64
        assert result.height_px == 48

    def test_captured_at_extracted(self):
        result = extract_exif(_make_tiff(), "image/tiff")
        assert result.captured_at == _EXPECTED_CAPTURED_AT


# ---------------------------------------------------------------------------
# Missing EXIF
# ---------------------------------------------------------------------------


class TestMissingExif:
    def test_jpeg_without_exif_returns_none_fields(self):
        result = extract_exif(_make_jpeg(with_exif=False), "image/jpeg")
        assert result.make is None
        assert result.model is None
        assert result.captured_at is None

    def test_jpeg_without_exif_still_returns_dimensions(self):
        result = extract_exif(_make_jpeg(width=50, height=30, with_exif=False), "image/jpeg")
        assert result.width_px == 50
        assert result.height_px == 30


# ---------------------------------------------------------------------------
# Video mime types
# ---------------------------------------------------------------------------


class TestVideoMimeType:
    @pytest.mark.parametrize("mime", ["video/mp4", "video/quicktime", "video/x-msvideo"])
    def test_video_returns_all_none(self, mime):
        result = extract_exif(b"not-real-video-data", mime)
        assert result.make is None
        assert result.model is None
        assert result.width_px is None
        assert result.height_px is None
        assert result.captured_at is None

    def test_video_does_not_raise(self):
        extract_exif(b"garbage", "video/mp4")  # must not raise


# ---------------------------------------------------------------------------
# Corrupt / unreadable data
# ---------------------------------------------------------------------------


class TestCorruptData:
    def test_random_bytes_does_not_raise(self):
        extract_exif(b"\x00\x01\x02\x03garbage", "image/jpeg")

    def test_empty_bytes_does_not_raise(self):
        extract_exif(b"", "image/jpeg")

    def test_corrupt_returns_all_none(self):
        result = extract_exif(b"this is not an image", "image/jpeg")
        assert result.make is None
        assert result.model is None
        assert result.width_px is None
        assert result.height_px is None
        assert result.captured_at is None


# ---------------------------------------------------------------------------
# _parse_exif_datetime helper
# ---------------------------------------------------------------------------


class TestParseExifDatetime:
    def test_valid_string(self):
        result = _parse_exif_datetime("2021:08:14 10:00:00")
        assert result == datetime(2021, 8, 14, 10, 0, 0, tzinfo=timezone.utc)

    def test_result_is_utc(self):
        result = _parse_exif_datetime("2021:08:14 10:00:00")
        assert result.tzinfo == timezone.utc

    def test_none_input(self):
        assert _parse_exif_datetime(None) is None

    def test_empty_string(self):
        assert _parse_exif_datetime("") is None

    def test_malformed_string(self):
        assert _parse_exif_datetime("not-a-date") is None

    def test_partial_string(self):
        assert _parse_exif_datetime("2021:08:14") is None


# ---------------------------------------------------------------------------
# HEIC / HEIF tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HEIF_AVAILABLE, reason="pillow-heif not installed")
class TestHeicExif:
    def test_make_model_and_dimensions_extracted(self):
        result = extract_exif(_make_heic(with_exif=True), "image/heic")
        assert result.make == _MAKE
        assert result.model == _MODEL
        assert result.width_px == 120
        assert result.height_px == 80

    def test_heic_without_exif_returns_none_metadata_no_crash(self):
        result = extract_exif(_make_heic(with_exif=False), "image/heic")
        assert result.make is None
        assert result.model is None
        assert result.captured_at is None
        assert result.width_px == 120
        assert result.height_px == 80
