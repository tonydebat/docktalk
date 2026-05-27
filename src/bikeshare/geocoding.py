"""Nominatim geocoding + nearest-station ranking.

Used by the resolution cascade when Gemini-driven name parsing finds no
station candidates. The rider's raw transcript is sent to Nominatim (no
API key required); the returned lat/lon is then matched against the
station catalogue to find nearby docks.

Per Nominatim's usage policy, calls are throttled to at most one per
second per process. Heavy callers should expect occasional ~1s waits.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from src.bikeshare.station_data import haversine_m

NOMINATIM_USER_AGENT = "docktalk/0.1 (Bike Share Toronto voice assistant)"
NOMINATIM_TIMEOUT_SECONDS = 5.0
_MIN_INTERVAL_SECONDS = 1.0

_throttle_lock = threading.Lock()
_last_call_at: float = 0.0


def _throttle() -> None:
    global _last_call_at
    with _throttle_lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL_SECONDS - (now - _last_call_at)
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


def _build_geolocator() -> Any:
    from geopy.geocoders import Nominatim

    return Nominatim(user_agent=NOMINATIM_USER_AGENT, timeout=NOMINATIM_TIMEOUT_SECONDS)


def _place_name(location: Any) -> str:
    """Return a short human-readable place label from a Nominatim hit."""
    raw = getattr(location, "raw", None) or {}
    address = raw.get("address") if isinstance(raw, dict) else None
    if isinstance(address, dict):
        for key in (
            "attraction",
            "tourism",
            "building",
            "amenity",
            "road",
            "neighbourhood",
            "suburb",
        ):
            value = address.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    display = getattr(location, "address", "") or ""
    if isinstance(display, str) and display.strip():
        return display.split(",", 1)[0].strip()
    return "your destination"


def _is_dockable(info: dict[str, Any]) -> bool:
    """A station counts as a viable backup target if it is active with >0 docks."""
    status = info.get("station_status", "active")
    if status != "active":
        return False
    docks = info.get("num_docks_available")
    try:
        return int(docks) > 0
    except (TypeError, ValueError):
        return False


def geocode_to_nearby_stations(
    transcript: str,
    stations: dict[str, dict[str, Any]],
    *,
    radius_m: int = 1500,
    k: int = 5,
    geolocator: Any | None = None,
) -> list[dict[str, Any]]:
    """Geocode the transcript and return nearby station candidates.

    Args:
        transcript: Rider's raw words. Sent verbatim to Nominatim — do not
            substitute Gemini's reformulation here, since the geocoder is
            often better at landmark resolution than name extraction.
        stations: Dict of station_id → merged info + live status, including
            ``lat``, ``lon``, ``name``, ``num_docks_available``,
            ``station_status``, and ``capacity``.
        radius_m: Maximum distance from the geocoded point to consider a
            station. Stations beyond this are excluded.
        k: Maximum number of nearby stations to return.
        geolocator: Optional injected Nominatim instance for testing.

    Returns:
        Up to ``k`` candidate dicts in the recommendation-object shape, or
        ``[]`` on geocoder miss, error, or when no dockable station falls
        within the radius.
    """
    if not transcript or not transcript.strip():
        return []

    if geolocator is None:
        try:
            geolocator = _build_geolocator()
        except Exception:
            return []

    _throttle()
    try:
        location = geolocator.geocode(
            transcript.strip(),
            country_codes="ca",
            addressdetails=True,
        )
    except Exception:
        return []

    if location is None:
        return []

    lat = getattr(location, "latitude", None)
    lon = getattr(location, "longitude", None)
    if lat is None or lon is None:
        return []

    place = _place_name(location)
    scored: list[tuple[float, dict[str, Any]]] = []

    for station_id, info in stations.items():
        s_lat = info.get("lat")
        s_lon = info.get("lon")
        if s_lat is None or s_lon is None:
            continue
        if not _is_dockable(info):
            continue
        try:
            distance = haversine_m(float(lat), float(lon), float(s_lat), float(s_lon))
        except (TypeError, ValueError):
            continue
        if distance > radius_m:
            continue
        scored.append((
            distance,
            {
                "station_id": station_id,
                "name": info.get("name", ""),
                "location_hint": info.get("name", ""),
                "available_docks": int(info.get("num_docks_available", 0)),
                "distance_meters": int(round(distance)),
                "station_status": info.get("station_status", "active"),
                "recommendation_reason": f"closest dock to {place}",
            },
        ))

    scored.sort(key=lambda pair: pair[0])
    return [candidate for _, candidate in scored[:k]]
