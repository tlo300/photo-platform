"""Nominatim reverse-geocoding service.

Single public function:
  reverse_geocode(lat, lon) -> str | None

Returns a city-level place name or None on failure.
Enforces >= 1.1 seconds between requests (Nominatim fair-use policy requires >= 1 s).
Uses only the Python standard library — no extra dependencies.
"""

import json
import logging
import threading
import time
import urllib.request

logger = logging.getLogger(__name__)

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
_USER_AGENT = "photo-platform/1.0 (self-hosted photo library)"
_MIN_INTERVAL = 1.1  # seconds between requests

_lock = threading.Lock()
_last_request_at: float = 0.0


def reverse_geocode(lat: float, lon: float) -> str | None:
    """Reverse-geocode (lat, lon) to a city-level place name via Nominatim.

    Extracts the best available city-level label from the address object,
    falling back through: city -> town -> village -> municipality -> county.
    As a last resort, returns the first comma-separated segment of display_name.

    Returns None when Nominatim returns no result or the request fails.
    Thread-safe; enforces >= 1.1 s between requests.
    """
    global _last_request_at

    url = (
        f"{_NOMINATIM_URL}?lat={lat}&lon={lon}"
        "&format=json&zoom=10&addressdetails=1"
    )
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})

    with _lock:
        elapsed = time.monotonic() - _last_request_at
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            _last_request_at = time.monotonic()
        except Exception as exc:
            _last_request_at = time.monotonic()
            logger.warning("Nominatim request failed (%.6f, %.6f): %s", lat, lon, exc)
            return None

    address = data.get("address") or {}
    for key in ("city", "town", "village", "municipality", "county"):
        name = address.get(key)
        if name:
            return str(name)

    # Last resort: first segment of display_name
    display = data.get("display_name") or ""
    if display:
        first = display.split(",")[0].strip()
        return first or None

    return None
