"""Unit tests for sidecar-missing handling (issue #41).

All tests are pure (no database, no object storage).

Scenarios covered:
  1. _mtime_from_zip_info — standard entry with a real timestamp
  2. _mtime_from_zip_info — seconds field clamped to valid range (some zip tools write 60)
  3. No sidecar + EXIF date present: merge returns the EXIF date (sidecar_missing path)
  4. No sidecar + no EXIF date: merge returns None (mtime fallback applied in worker)
"""

import zipfile
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from app.services.exif import ExifResult
from app.services.metadata_merge import merge_metadata
from app.worker.takeout_tasks import _mtime_from_zip_info


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_zip_info(
    filename: str = "photo.jpg",
    date_time: tuple = (2022, 7, 4, 14, 30, 20),
) -> zipfile.ZipInfo:
    """Return a ZipInfo with a controlled date_time tuple."""
    info = zipfile.ZipInfo(filename=filename, date_time=date_time)
    return info


def _exif_with_date(year: int = 2022) -> ExifResult:
    return ExifResult(
        captured_at=datetime(year, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
        make="Canon",
        model="EOS R5",
        width_px=8192,
        height_px=5464,
    )


def _exif_no_date() -> ExifResult:
    return ExifResult(
        captured_at=None,
        make=None,
        model=None,
        width_px=None,
        height_px=None,
    )


# ---------------------------------------------------------------------------
# _mtime_from_zip_info — pure helper
# ---------------------------------------------------------------------------


def test_mtime_from_zip_info_standard():
    """Standard entry: date_time is parsed and returned as a UTC datetime."""
    info = _make_zip_info(date_time=(2022, 7, 4, 14, 30, 20))
    result = _mtime_from_zip_info(info)

    assert isinstance(result, datetime)
    assert result.tzinfo == timezone.utc
    assert result.year == 2022
    assert result.month == 7
    assert result.day == 4
    assert result.hour == 14
    assert result.minute == 30
    assert result.second == 20


def test_mtime_from_zip_info_clamps_seconds():
    """seconds=60 (written by some zip tools) is clamped to 59."""
    info = _make_zip_info(date_time=(2021, 3, 10, 8, 0, 60))
    result = _mtime_from_zip_info(info)

    assert result.second == 59


# ---------------------------------------------------------------------------
# Sidecar-missing: merge behaviour (pure function)
# ---------------------------------------------------------------------------


def test_no_sidecar_exif_date_used():
    """No sidecar present + EXIF has a date: merge_metadata returns the EXIF date."""
    exif = _exif_with_date(2020)
    result = merge_metadata(exif, None)

    assert result.captured_at == datetime(2020, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    assert result.make == "Canon"
    assert result.raw_json is None


def test_no_sidecar_no_exif_captured_at_is_none():
    """No sidecar, no EXIF date: merge_metadata returns None.

    The worker applies an mtime fallback after this; the pure merge result is NULL
    so that the fallback logic has a clear signal to act on.
    """
    exif = _exif_no_date()
    result = merge_metadata(exif, None)

    assert result.captured_at is None
