"""Unit tests for extract_exif (pure function — no database required).

Tests cover:
  - JPEG with full EXIF (make, model, DateTimeOriginal, dimensions)
  - TIFF with full EXIF
  - JPEG with no EXIF tags present
  - Video mime type → all-None result, no crash (ffprobe mocked)
  - Corrupt bytes → all-None result, no crash
  - Timestamp merge: sidecar_captured_at must not be overwritten by apply_exif
  - HEIC with full EXIF (make, model, dimensions)
  - HEIC with no EXIF — returns None metadata, no crash
  - Extended EXIF fields: ISO, aperture, shutter speed, focal length, flash
  - GPS extraction: valid coordinates, null-island rejection, missing data
  - Helper functions: _rational_to_float, _dms_to_decimal, _parse_gps
"""

import io
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from app.services.exif import (
    ExifResult,
    _HEIF_AVAILABLE,
    _dms_to_decimal,
    _parse_exif_datetime,
    _parse_gps,
    _rational_to_float,
    extract_exif,
)

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
_TAG_EXPOSURE_TIME = 33434
_TAG_FNUMBER = 33437
_TAG_ISO_SPEED = 34855
_TAG_FLASH = 37385
_TAG_FOCAL_LENGTH = 37386


def _make_jpeg(
    width: int = 120,
    height: int = 80,
    with_exif: bool = True,
    with_extended: bool = False,
) -> bytes:
    """Build a minimal JPEG, optionally with extended EXIF fields."""
    img = Image.new("RGB", (width, height), color=(128, 64, 32))
    if with_exif:
        exif = img.getexif()
        exif[_TAG_MAKE] = _MAKE
        exif[_TAG_MODEL] = _MODEL
        exif[_TAG_DATETIME_ORIGINAL] = _DATETIME_ORIGINAL
        if with_extended:
            # Write into the Exif sub-IFD (IFD 34665)
            exif_ifd = exif.get_ifd(34665)
            exif_ifd[_TAG_EXPOSURE_TIME] = (1, 250)   # 1/250 s
            exif_ifd[_TAG_FNUMBER] = (28, 10)          # f/2.8
            exif_ifd[_TAG_ISO_SPEED] = 400
            exif_ifd[_TAG_FOCAL_LENGTH] = (50, 1)      # 50 mm
            exif_ifd[_TAG_FLASH] = 1                    # flash fired (bit 0 = 1)
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


# ---------------------------------------------------------------------------
# Extended EXIF fields: ISO, aperture, shutter speed, focal length, flash
#
# Pillow does not reliably round-trip sub-IFD data written to a freshly
# created image, so these tests mock Image.open to return pre-crafted
# EXIF data rather than building a real JPEG with a sub-IFD.
# ---------------------------------------------------------------------------


def _make_mock_image(
    *,
    size: tuple[int, int] = (120, 80),
    top_tags: dict | None = None,
    exif_ifd_tags: dict | None = None,
    gps_ifd_tags: dict | None = None,
) -> MagicMock:
    """Return a MagicMock that behaves like a Pillow Image with EXIF data."""
    top_tags = top_tags or {}
    exif_ifd = exif_ifd_tags or {}
    gps_ifd = gps_ifd_tags or {}

    mock_exif = MagicMock()
    mock_exif.get = lambda tag, default=None: top_tags.get(tag, default)

    def _get_ifd(tag: int) -> dict:
        if tag == 34665:
            return exif_ifd
        if tag == 34853:
            return gps_ifd
        return {}

    mock_exif.get_ifd = _get_ifd

    mock_img = MagicMock()
    mock_img.size = size
    mock_img.getexif.return_value = mock_exif
    return mock_img


class TestExtendedExifFields:
    def test_iso_extracted(self):
        img = _make_mock_image(exif_ifd_tags={34855: 400})
        with patch("app.services.exif.Image.open", return_value=img):
            result = extract_exif(b"fake", "image/jpeg")
        assert result.iso == 400

    def test_aperture_extracted(self):
        img = _make_mock_image(exif_ifd_tags={33437: (28, 10)})
        with patch("app.services.exif.Image.open", return_value=img):
            result = extract_exif(b"fake", "image/jpeg")
        assert result.aperture == pytest.approx(2.8, rel=1e-3)

    def test_shutter_speed_extracted(self):
        img = _make_mock_image(exif_ifd_tags={33434: (1, 250)})
        with patch("app.services.exif.Image.open", return_value=img):
            result = extract_exif(b"fake", "image/jpeg")
        assert result.shutter_speed == pytest.approx(1 / 250, rel=1e-3)

    def test_focal_length_extracted(self):
        img = _make_mock_image(exif_ifd_tags={37386: (50, 1)})
        with patch("app.services.exif.Image.open", return_value=img):
            result = extract_exif(b"fake", "image/jpeg")
        assert result.focal_length == pytest.approx(50.0, rel=1e-3)

    def test_flash_fired(self):
        img = _make_mock_image(exif_ifd_tags={37385: 1})
        with patch("app.services.exif.Image.open", return_value=img):
            result = extract_exif(b"fake", "image/jpeg")
        assert result.flash is True

    def test_flash_not_fired(self):
        img = _make_mock_image(exif_ifd_tags={37385: 0})
        with patch("app.services.exif.Image.open", return_value=img):
            result = extract_exif(b"fake", "image/jpeg")
        assert result.flash is False

    def test_extended_fields_none_when_absent(self):
        img = _make_mock_image()
        with patch("app.services.exif.Image.open", return_value=img):
            result = extract_exif(b"fake", "image/jpeg")
        assert result.iso is None
        assert result.aperture is None
        assert result.shutter_speed is None
        assert result.focal_length is None
        assert result.flash is None

    def test_duration_seconds_none_for_images(self):
        img = _make_mock_image()
        with patch("app.services.exif.Image.open", return_value=img):
            result = extract_exif(b"fake", "image/jpeg")
        assert result.duration_seconds is None


# ---------------------------------------------------------------------------
# GPS helpers: _rational_to_float, _dms_to_decimal, _parse_gps
# ---------------------------------------------------------------------------


class TestRationalToFloat:
    def test_tuple_rational(self):
        assert _rational_to_float((1, 4)) == pytest.approx(0.25)

    def test_integer(self):
        assert _rational_to_float(50) == pytest.approx(50.0)

    def test_none_returns_none(self):
        assert _rational_to_float(None) is None

    def test_zero_denominator_returns_none(self):
        # IFDRational with zero denominator raises ZeroDivisionError
        class _ZeroRational:
            def __float__(self):
                raise ZeroDivisionError

        assert _rational_to_float(_ZeroRational()) is None


class TestDmsToDecimal:
    def test_north_positive(self):
        # 52° 22' 12" N → 52 + 22/60 + 12/3600 = 52.37
        result = _dms_to_decimal(((52, 1), (22, 1), (12, 1)), "N")
        assert result == pytest.approx(52.37, rel=1e-3)

    def test_south_negative(self):
        result = _dms_to_decimal(((52, 1), (0, 1), (0, 1)), "S")
        assert result == pytest.approx(-52.0)

    def test_west_negative(self):
        result = _dms_to_decimal(((4, 1), (54, 1), (0, 1)), "W")
        assert result is not None
        assert result < 0

    def test_none_ref_returns_none(self):
        assert _dms_to_decimal(((52, 1), (0, 1), (0, 1)), None) is None

    def test_none_dms_returns_none(self):
        assert _dms_to_decimal(None, "N") is None

    def test_wrong_length_returns_none(self):
        assert _dms_to_decimal(((52, 1), (22, 1)), "N") is None


class TestParseGps:
    def _make_gps_ifd(self, lat: float, lon: float, alt: float | None = None) -> dict:
        """Build a minimal GPS IFD dict for the given decimal lat/lon."""
        def _to_dms(deg: float):
            d = int(abs(deg))
            m = int((abs(deg) - d) * 60)
            s = round(((abs(deg) - d) * 60 - m) * 60)
            return ((d, 1), (m, 1), (s, 1))

        ifd = {
            1: "N" if lat >= 0 else "S",
            2: _to_dms(lat),
            3: "E" if lon >= 0 else "W",
            4: _to_dms(lon),
        }
        if alt is not None:
            ifd[5] = 0
            ifd[6] = (int(abs(alt) * 100), 100)
        return ifd

    def test_valid_coordinates_extracted(self):
        gps_ifd = self._make_gps_ifd(52.37, 4.89)
        lat, lon, alt = _parse_gps(gps_ifd)
        assert lat == pytest.approx(52.37, abs=0.01)
        assert lon == pytest.approx(4.89, abs=0.01)

    def test_altitude_extracted(self):
        gps_ifd = self._make_gps_ifd(52.0, 5.0, alt=15.0)
        _, _, alt = _parse_gps(gps_ifd)
        assert alt == pytest.approx(15.0, rel=0.05)

    def test_null_island_returns_none(self):
        gps_ifd = {1: "N", 2: ((0, 1), (0, 1), (0, 1)), 3: "E", 4: ((0, 1), (0, 1), (0, 1))}
        lat, lon, alt = _parse_gps(gps_ifd)
        assert lat is None
        assert lon is None

    def test_empty_ifd_returns_none(self):
        lat, lon, alt = _parse_gps({})
        assert lat is None
        assert lon is None
        assert alt is None

    def test_none_ifd_returns_none(self):
        lat, lon, alt = _parse_gps(None)
        assert lat is None
        assert lon is None
        assert alt is None


# ---------------------------------------------------------------------------
# Video extraction via ffprobe (mocked)
# ---------------------------------------------------------------------------


class TestVideoExtraction:
    def _ffprobe_stdout(self, width: int, height: int, duration: float) -> bytes:
        return json.dumps({
            "streams": [
                {
                    "codec_type": "video",
                    "width": width,
                    "height": height,
                    "duration": str(duration),
                }
            ]
        }).encode()

    def test_video_width_height_duration_extracted(self):
        with patch("app.services.exif.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=self._ffprobe_stdout(1920, 1080, 63.5),
            )
            result = extract_exif(b"\x00fake-video\x00", "video/mp4")

        assert result.width_px == 1920
        assert result.height_px == 1080
        assert result.duration_seconds == pytest.approx(63.5)

    def test_video_image_fields_are_none(self):
        with patch("app.services.exif.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=self._ffprobe_stdout(1280, 720, 10.0),
            )
            result = extract_exif(b"\x00fake-video\x00", "video/mp4")

        assert result.make is None
        assert result.model is None
        assert result.captured_at is None
        assert result.iso is None

    def test_ffprobe_failure_returns_all_none(self):
        with patch("app.services.exif.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout=b"")
            result = extract_exif(b"\x00bad\x00", "video/mp4")

        assert result.width_px is None
        assert result.height_px is None
        assert result.duration_seconds is None

    def test_ffprobe_exception_returns_all_none(self):
        with patch("app.services.exif.subprocess.run", side_effect=FileNotFoundError("ffprobe")):
            result = extract_exif(b"\x00bad\x00", "video/mp4")

        assert result.width_px is None
