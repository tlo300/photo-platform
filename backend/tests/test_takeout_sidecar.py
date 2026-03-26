"""Unit tests for the Google Takeout sidecar parser (parse_sidecar only).

All tests are pure — no database required.
"""

from datetime import datetime, timezone

import pytest

from app.services.takeout_sidecar import parse_sidecar


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FULL_SIDECAR = {
    "title": "IMG_20210814_123456.jpg",
    "description": "Summer holiday",
    "photoTakenTime": {"timestamp": "1628940000", "formatted": "14 Aug 2021, 10:00:00 UTC"},
    "creationTime": {"timestamp": "1628940001", "formatted": "14 Aug 2021, 10:00:01 UTC"},
    "geoData": {
        "latitude": 52.3702,
        "longitude": 4.8952,
        "altitude": 3.5,
        "latitudeSpan": 0.001,
        "longitudeSpan": 0.001,
    },
    "geoDataExif": {
        "latitude": 52.3702,
        "longitude": 4.8952,
        "altitude": 3.5,
        "latitudeSpan": 0.001,
        "longitudeSpan": 0.001,
    },
    "people": [{"name": "Alice"}, {"name": "Bob"}],
    "googlePhotosOrigin": {"mobileUpload": {"deviceFolder": {"localFolderName": ""}}},
}


# ---------------------------------------------------------------------------
# Full sidecar
# ---------------------------------------------------------------------------

class TestFullSidecar:
    def test_captured_at_uses_photo_taken_time(self):
        result = parse_sidecar(FULL_SIDECAR)
        expected = datetime.fromtimestamp(1628940000, tz=timezone.utc)
        assert result.captured_at == expected

    def test_geo_extracted(self):
        result = parse_sidecar(FULL_SIDECAR)
        assert result.has_geo is True
        assert result.latitude == pytest.approx(52.3702)
        assert result.longitude == pytest.approx(4.8952)
        assert result.altitude_metres == pytest.approx(3.5)

    def test_description_extracted(self):
        result = parse_sidecar(FULL_SIDECAR)
        assert result.description == "Summer holiday"

    def test_people_extracted(self):
        result = parse_sidecar(FULL_SIDECAR)
        assert result.people == ["Alice", "Bob"]

    def test_raw_stored_verbatim(self):
        result = parse_sidecar(FULL_SIDECAR)
        assert result.raw is FULL_SIDECAR


# ---------------------------------------------------------------------------
# Missing geo
# ---------------------------------------------------------------------------

class TestMissingGeo:
    def test_missing_geo_data_key(self):
        raw = {**FULL_SIDECAR}
        del raw["geoData"]
        result = parse_sidecar(raw)
        assert result.has_geo is False
        assert result.latitude is None
        assert result.longitude is None
        assert result.altitude_metres is None

    def test_zero_lat_lon_treated_as_no_geo(self):
        raw = {
            **FULL_SIDECAR,
            "geoData": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
        }
        result = parse_sidecar(raw)
        assert result.has_geo is False

    def test_empty_geo_data_dict(self):
        raw = {**FULL_SIDECAR, "geoData": {}}
        result = parse_sidecar(raw)
        assert result.has_geo is False


# ---------------------------------------------------------------------------
# Missing people
# ---------------------------------------------------------------------------

class TestMissingPeople:
    def test_missing_people_key(self):
        raw = {**FULL_SIDECAR}
        del raw["people"]
        result = parse_sidecar(raw)
        assert result.people == []

    def test_empty_people_list(self):
        raw = {**FULL_SIDECAR, "people": []}
        result = parse_sidecar(raw)
        assert result.people == []

    def test_people_entry_missing_name(self):
        raw = {**FULL_SIDECAR, "people": [{"name": "Alice"}, {}]}
        result = parse_sidecar(raw)
        assert result.people == ["Alice"]

    def test_people_entry_blank_name(self):
        raw = {**FULL_SIDECAR, "people": [{"name": "  "}]}
        result = parse_sidecar(raw)
        assert result.people == []


# ---------------------------------------------------------------------------
# Missing timestamp
# ---------------------------------------------------------------------------

class TestMissingTimestamp:
    def test_missing_photo_taken_time_falls_back_to_creation_time(self):
        raw = {**FULL_SIDECAR}
        del raw["photoTakenTime"]
        result = parse_sidecar(raw)
        expected = datetime.fromtimestamp(1628940001, tz=timezone.utc)
        assert result.captured_at == expected

    def test_missing_both_timestamps_gives_none(self):
        raw = {**FULL_SIDECAR}
        del raw["photoTakenTime"]
        del raw["creationTime"]
        result = parse_sidecar(raw)
        assert result.captured_at is None

    def test_malformed_timestamp_gives_none(self):
        raw = {**FULL_SIDECAR, "photoTakenTime": {"timestamp": "not-a-number"}}
        del raw["creationTime"]
        result = parse_sidecar(raw)
        assert result.captured_at is None

    def test_millisecond_timestamp_parsed_correctly(self):
        # Some Takeout exports store timestamps in milliseconds (e.g. old 2003 photos).
        # 1067040000000 ms == 1067040000 s == 2003-10-25T00:00:00Z
        raw = {**FULL_SIDECAR, "photoTakenTime": {"timestamp": "1067040000000"}}
        result = parse_sidecar(raw)
        expected = datetime.fromtimestamp(1067040000, tz=timezone.utc)
        assert result.captured_at == expected


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

class TestRobustness:
    def test_empty_dict_does_not_raise(self):
        result = parse_sidecar({})
        assert result.captured_at is None
        assert result.has_geo is False
        assert result.people == []
        assert result.description is None

    def test_description_whitespace_only_treated_as_none(self):
        raw = {**FULL_SIDECAR, "description": "   "}
        result = parse_sidecar(raw)
        assert result.description is None

    def test_null_geo_data_value(self):
        raw = {**FULL_SIDECAR, "geoData": None}
        result = parse_sidecar(raw)
        assert result.has_geo is False
