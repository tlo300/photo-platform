"""Tests for reverse-geocoding service and Celery tasks (issue #125).

All HTTP calls and DB helpers are mocked — no live services required.

Tests cover:
  1.  reverse_geocode extracts city from address.city
  2.  reverse_geocode falls back through town → village → municipality → county
  3.  reverse_geocode falls back to first segment of display_name
  4.  reverse_geocode returns None when response has no usable name
  5.  reverse_geocode returns None on HTTP error
  6.  resolve_asset_geocode happy path — calls geocoder + updates DB
  7.  resolve_asset_geocode skips DB update when geocoder returns None
  8.  backfill_user_geocode dispatches one task per ungeocoded row
  9.  backfill_user_geocode is a no-op when all locations are already geocoded
"""

from __future__ import annotations

import json
import uuid
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_nominatim_response(address: dict, display_name: str = "") -> bytes:
    return json.dumps({"address": address, "display_name": display_name}).encode()


def _mock_urlopen(body: bytes):
    """Return a context-manager mock that yields a file-like object."""
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=BytesIO(body))
    cm.__exit__ = MagicMock(return_value=False)
    return cm


# ---------------------------------------------------------------------------
# geocoding.reverse_geocode — unit tests (no rate-limit sleep)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Suppress rate-limit sleep in all geocoding tests."""
    monkeypatch.setattr("app.services.geocoding.time.sleep", lambda _: None)
    # Reset the last-request timestamp so no sleep is triggered
    import app.services.geocoding as gc_mod
    monkeypatch.setattr(gc_mod, "_last_request_at", 0.0)


def test_reverse_geocode_city():
    body = _make_nominatim_response({"city": "Amsterdam", "country": "Netherlands"})
    with patch("app.services.geocoding.urllib.request.urlopen", return_value=_mock_urlopen(body)):
        from app.services.geocoding import reverse_geocode
        result = reverse_geocode(52.3676, 4.9041)
    assert result == "Amsterdam"


def test_reverse_geocode_fallback_town():
    body = _make_nominatim_response({"town": "Zaandam", "country": "Netherlands"})
    with patch("app.services.geocoding.urllib.request.urlopen", return_value=_mock_urlopen(body)):
        from app.services.geocoding import reverse_geocode
        result = reverse_geocode(52.4397, 4.8136)
    assert result == "Zaandam"


def test_reverse_geocode_fallback_village():
    body = _make_nominatim_response({"village": "Broek in Waterland"})
    with patch("app.services.geocoding.urllib.request.urlopen", return_value=_mock_urlopen(body)):
        from app.services.geocoding import reverse_geocode
        result = reverse_geocode(52.4380, 4.9955)
    assert result == "Broek in Waterland"


def test_reverse_geocode_fallback_display_name():
    body = _make_nominatim_response({}, display_name="Some Place, Region, Country")
    with patch("app.services.geocoding.urllib.request.urlopen", return_value=_mock_urlopen(body)):
        from app.services.geocoding import reverse_geocode
        result = reverse_geocode(0.0, 0.0)
    assert result == "Some Place"


def test_reverse_geocode_returns_none_when_no_name():
    body = json.dumps({"address": {}, "display_name": ""}).encode()
    with patch("app.services.geocoding.urllib.request.urlopen", return_value=_mock_urlopen(body)):
        from app.services.geocoding import reverse_geocode
        result = reverse_geocode(0.0, 0.0)
    assert result is None


def test_reverse_geocode_returns_none_on_http_error():
    with patch(
        "app.services.geocoding.urllib.request.urlopen",
        side_effect=OSError("connection refused"),
    ):
        from app.services.geocoding import reverse_geocode
        result = reverse_geocode(52.0, 4.0)
    assert result is None


# ---------------------------------------------------------------------------
# geocode_tasks.resolve_asset_geocode
# ---------------------------------------------------------------------------


def test_resolve_asset_geocode_happy_path():
    asset_id = str(uuid.uuid4())
    owner_id = str(uuid.uuid4())

    with (
        patch(
            "app.worker.geocode_tasks.reverse_geocode",
            return_value="Rotterdam",
        ),
        patch(
            "app.worker.geocode_tasks._update_display_name",
            new=AsyncMock(),
        ) as mock_update,
    ):
        from app.worker.geocode_tasks import resolve_asset_geocode
        resolve_asset_geocode(asset_id, owner_id, 51.9225, 4.4792)

    mock_update.assert_awaited_once_with(
        uuid.UUID(asset_id), uuid.UUID(owner_id), "Rotterdam"
    )


def test_resolve_asset_geocode_skips_when_no_result():
    asset_id = str(uuid.uuid4())
    owner_id = str(uuid.uuid4())

    with (
        patch("app.worker.geocode_tasks.reverse_geocode", return_value=None),
        patch(
            "app.worker.geocode_tasks._update_display_name",
            new=AsyncMock(),
        ) as mock_update,
    ):
        from app.worker.geocode_tasks import resolve_asset_geocode
        resolve_asset_geocode(asset_id, owner_id, 0.0, 0.0)

    mock_update.assert_not_awaited()


# ---------------------------------------------------------------------------
# geocode_tasks.backfill_user_geocode
# ---------------------------------------------------------------------------


def test_backfill_user_geocode_dispatches_per_row():
    owner_id = str(uuid.uuid4())
    asset1 = uuid.uuid4()
    asset2 = uuid.uuid4()
    rows = [(asset1, 52.37, 4.90), (asset2, 51.92, 4.48)]

    with (
        patch(
            "app.worker.geocode_tasks._get_ungeocoded_locations",
            new=AsyncMock(return_value=rows),
        ),
        patch("app.worker.geocode_tasks.resolve_asset_geocode") as mock_task,
    ):
        mock_task.delay = MagicMock()
        from app.worker.geocode_tasks import backfill_user_geocode
        backfill_user_geocode(owner_id)

    assert mock_task.delay.call_count == 2
    mock_task.delay.assert_any_call(str(asset1), owner_id, 52.37, 4.90)
    mock_task.delay.assert_any_call(str(asset2), owner_id, 51.92, 4.48)


def test_backfill_user_geocode_noop_when_all_geocoded():
    owner_id = str(uuid.uuid4())

    with (
        patch(
            "app.worker.geocode_tasks._get_ungeocoded_locations",
            new=AsyncMock(return_value=[]),
        ),
        patch("app.worker.geocode_tasks.resolve_asset_geocode") as mock_task,
    ):
        mock_task.delay = MagicMock()
        from app.worker.geocode_tasks import backfill_user_geocode
        backfill_user_geocode(owner_id)

    mock_task.delay.assert_not_called()


