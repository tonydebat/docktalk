import json
from typing import Any

from .cache import fetch_and_cache

STATION_INFORMATION_URL = (
    "https://tor.publicbikesystem.net/ube/gbfs/v1/en/station_information"
)


def fetch_station_information() -> dict[str, Any]:
    """Download the Bike Share Toronto station information feed to a temp file.

    Returns {"cache_path": "/tmp/..."} on success, or an error dict on failure.
    Pass the returned cache_path to get_station_information to look up individual stations.
    """
    return fetch_and_cache(STATION_INFORMATION_URL, prefix="docktalk_station_info_")


def get_station_information(station_id: str, cache_path: str) -> dict[str, Any]:
    """Return metadata (name, lat, lon, capacity, address) for a single station.

    Args:
        station_id: The GBFS station_id to look up.
        cache_path: Path returned by fetch_station_information.

    Returns the station dict or an error dict on failure.
    """
    try:
        with open(cache_path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {
            "is_error": True,
            "content": f"FileNotFoundError: cache file '{cache_path}' not found",
        }
    except (OSError, ValueError) as exc:
        return {"is_error": True, "content": f"{type(exc).__name__}: {exc}"}

    for station in data.get("data", {}).get("stations", []):
        if station.get("station_id") == station_id:
            return station

    return {
        "is_error": True,
        "content": f"StationNotFound: station_id '{station_id}' not found in information feed",
    }
