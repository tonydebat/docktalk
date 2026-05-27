"""Orchestrates the destination-resolution cascade.

Step 1: Gemini parses the transcript into ranked search terms; each is
        tried against ``search_stations`` until one returns matches.
Step 2: Nominatim geocodes the rider's raw transcript; the K nearest
        dockable stations within 1500 m are returned.

Step 3 (clarification) lives in the Streamlit UI layer; this module only
covers the two deterministic steps.

All results are normalised to the recommendation-object shape from the
station recommendation contract, so the UI renders a single shape
regardless of which step produced the candidates.
"""

from __future__ import annotations

from typing import Any, Callable

from src.bikeshare.geocoding import geocode_to_nearby_stations
from src.bikeshare.parsing import parse_destination_intent
from src.bikeshare.station_search import search_stations

MAX_CANDIDATES = 5
GEOCODE_RADIUS_M = 1500


def _normalise_search_hit(
    hit: dict[str, Any],
    *,
    term: str,
) -> dict[str, Any]:
    """Shape a ``search_stations`` result into the recommendation-object schema."""
    name = hit.get("name", "")
    return {
        "station_id": hit.get("station_id", ""),
        "name": name,
        "location_hint": name,
        "available_docks": int(hit.get("num_docks_available", 0) or 0),
        "distance_meters": int(hit.get("distance_meters", 0) or 0),
        "station_status": hit.get("station_status", "active"),
        "recommendation_reason": f"name match for '{term}'",
    }


def resolve_destination(
    transcript: str,
    stations: dict[str, dict[str, Any]],
    *,
    parse_intent: Callable[[str], list[str]] = parse_destination_intent,
    geocode: Callable[..., list[dict[str, Any]]] = geocode_to_nearby_stations,
    search: Callable[[str, dict[str, dict[str, Any]]], list[dict[str, Any]]] = search_stations,
) -> list[dict[str, Any]]:
    """Run the resolution cascade for a single rider transcript.

    Args:
        transcript: The rider's raw Whisper transcript. This exact string
            is what gets geocoded in Step 2 (Gemini's reformulation is not
            used for geocoding — preserve the rider's actual words).
        stations: Dict of station_id → merged info + live status.
        parse_intent: Override for Step-1 Gemini call (test seam).
        geocode: Override for Step-2 Nominatim call (test seam).
        search: Override for the name-match search (test seam).

    Returns:
        Up to :data:`MAX_CANDIDATES` candidate dicts in the
        recommendation-object shape, or ``[]`` if both Step 1 and Step 2
        produce no candidates. The caller is then responsible for invoking
        the clarification loop.
    """
    if not transcript or not transcript.strip():
        return []

    cleaned = transcript.strip()

    # ── Step 1: ranked name-match terms ───────────────────────────────
    terms = parse_intent(cleaned)
    for term in terms:
        hits = search(term, stations)
        if hits:
            normalised = [_normalise_search_hit(h, term=term) for h in hits]
            return normalised[:MAX_CANDIDATES]

    # ── Step 2: geocoding fallback ────────────────────────────────────
    geocoded = geocode(
        cleaned,
        stations,
        radius_m=GEOCODE_RADIUS_M,
        k=MAX_CANDIDATES,
    )
    if geocoded:
        return geocoded[:MAX_CANDIDATES]

    return []


def merge_info_and_status(
    info: dict[str, dict[str, Any]],
    status: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Compose the dict shape ``resolve_destination`` expects.

    ``fetch_all_stations()`` returns metadata (lat/lon/name/capacity) and
    ``fetch_live_status()`` returns live dock counts; the resolver and
    geocoder need both in a single dict per station.
    """
    merged: dict[str, dict[str, Any]] = {}
    for station_id, meta in info.items():
        live = status.get(station_id, {})
        merged[station_id] = {
            **meta,
            "num_docks_available": live.get("num_docks_available", 0),
            "num_bikes_available": live.get("num_bikes_available", 0),
            "station_status": live.get("station_status", "active"),
            "is_returning": live.get("is_returning", 0),
        }
    return merged
