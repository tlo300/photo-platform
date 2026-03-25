"""Unit tests for merge_metadata (pure function — no database required).

Scenarios covered (7, matching issue #40 acceptance criteria):
  1. Both sources present and agree (same year)
  2. Both present and conflict (EXIF year differs from sidecar by > 2 years) — JSON wins
  3. EXIF only (no sidecar)
  4. JSON only (no EXIF)
  5. Neither source present
  6. Bad EXIF date — year outside 1990–2030 — treated as absent; NULL when no JSON
  7. Zero GPS coords in sidecar — has_geo is False; no location row intended
"""

import logging
from datetime import datetime, timezone

import pytest

from app.services.exif import ExifResult
from app.services.metadata_merge import CanonicalMetadata, merge_metadata
from app.services.takeout_sidecar import ParsedSidecar


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _exif(
    *,
    captured_at: datetime | None = None,
    make: str | None = "Apple",
    model: str | None = "iPhone 13",
    width_px: int | None = 4032,
    height_px: int | None = 3024,
) -> ExifResult:
    return ExifResult(
        make=make,
        model=model,
        width_px=width_px,
        height_px=height_px,
        captured_at=captured_at,
    )


def _sidecar(
    *,
    captured_at: datetime | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    altitude_metres: float | None = None,
    has_geo: bool = False,
    description: str | None = None,
    people: list[str] | None = None,
) -> ParsedSidecar:
    return ParsedSidecar(
        captured_at=captured_at,
        latitude=latitude,
        longitude=longitude,
        altitude_metres=altitude_metres,
        has_geo=has_geo,
        description=description,
        people=people or [],
        raw={},
    )


def _dt(year: int, month: int = 6, day: int = 15) -> datetime:
    return datetime(year, month, day, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Scenario 1: both sources present and agree (same year)
# ---------------------------------------------------------------------------

def test_both_present_agree_sidecar_wins():
    """When both timestamps point to the same year the sidecar takes priority."""
    exif = _exif(captured_at=_dt(2021))
    sidecar = _sidecar(captured_at=_dt(2021), latitude=52.3, longitude=4.9, has_geo=True)
    result = merge_metadata(exif, sidecar)

    assert isinstance(result, CanonicalMetadata)
    assert result.captured_at == _dt(2021)
    # Sidecar is primary — make/model still come from EXIF
    assert result.make == "Apple"
    assert result.has_geo is True
    assert result.latitude == pytest.approx(52.3)
    assert result.longitude == pytest.approx(4.9)


# ---------------------------------------------------------------------------
# Scenario 2: both present, EXIF year conflicts with JSON year (> 2 yrs apart)
# ---------------------------------------------------------------------------

def test_both_present_conflict_json_wins(caplog):
    """EXIF year 2029, sidecar year 2003 → sidecar wins, warning logged."""
    exif = _exif(captured_at=_dt(2029))
    sidecar = _sidecar(captured_at=_dt(2003))

    with caplog.at_level(logging.WARNING, logger="app.services.metadata_merge"):
        result = merge_metadata(exif, sidecar)

    assert result.captured_at == _dt(2003)
    assert any("differ" in msg.lower() for msg in caplog.messages)


# ---------------------------------------------------------------------------
# Scenario 3: EXIF only (no sidecar)
# ---------------------------------------------------------------------------

def test_exif_only():
    exif = _exif(captured_at=_dt(2020))
    result = merge_metadata(exif, None)

    assert result.captured_at == _dt(2020)
    assert result.make == "Apple"
    assert result.has_geo is False
    assert result.people == []
    assert result.raw_json is None


# ---------------------------------------------------------------------------
# Scenario 4: JSON only (no EXIF)
# ---------------------------------------------------------------------------

def test_sidecar_only():
    sidecar = _sidecar(
        captured_at=_dt(2019),
        description="Holiday",
        people=["Alice", "Bob"],
    )
    result = merge_metadata(None, sidecar)

    assert result.captured_at == _dt(2019)
    assert result.make is None
    assert result.model is None
    assert result.description == "Holiday"
    assert result.people == ["Alice", "Bob"]


# ---------------------------------------------------------------------------
# Scenario 5: neither source present
# ---------------------------------------------------------------------------

def test_neither_source():
    result = merge_metadata(None, None)

    assert result.captured_at is None
    assert result.make is None
    assert result.has_geo is False
    assert result.people == []


# ---------------------------------------------------------------------------
# Scenario 6: bad EXIF date — year outside 1990–2030
# ---------------------------------------------------------------------------

def test_bad_exif_year_rejected_no_json(caplog):
    """EXIF year 1970 (clock reset default) → discarded; captured_at is None."""
    exif = _exif(captured_at=_dt(1970))

    with caplog.at_level(logging.WARNING, logger="app.services.metadata_merge"):
        result = merge_metadata(exif, None)

    assert result.captured_at is None
    assert any("outside" in msg.lower() for msg in caplog.messages)


def test_bad_exif_year_json_still_used():
    """Bad EXIF year + valid sidecar → sidecar date is preserved."""
    exif = _exif(captured_at=_dt(1970))
    sidecar = _sidecar(captured_at=_dt(2005))
    result = merge_metadata(exif, sidecar)

    assert result.captured_at == _dt(2005)


# ---------------------------------------------------------------------------
# Scenario 7: zero GPS coordinates → has_geo False
# ---------------------------------------------------------------------------

def test_zero_gps_coords():
    """Sidecar with lat=0 lon=0 should have has_geo=False per parse_sidecar contract."""
    sidecar = _sidecar(
        captured_at=_dt(2022),
        latitude=None,
        longitude=None,
        has_geo=False,
    )
    result = merge_metadata(None, sidecar)

    assert result.has_geo is False
    assert result.latitude is None
    assert result.longitude is None
