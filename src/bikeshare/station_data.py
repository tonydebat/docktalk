import json
import math
import time
from datetime import datetime, timezone
from typing import Any
from urllib.request import urlopen


STATION_STATUS_URL = (
    "https://toronto.publicbikesystem.net/customer/gbfs/v3.0/station_status"
)
STATION_INFO_URL = (
    "https://toronto.publicbikesystem.net/customer/gbfs/v3.0/station_information"
)

_STATUS_CACHE_TTL = 30  # seconds, shared across tool calls within a single agent tick

_info_cache: dict[str, dict[str, Any]] | None = None
_status_cache: tuple[float, dict[str, dict[str, Any]]] | None = None


def _extract_name(name_field: Any) -> str:
    """GBFS v3 returns name as a multilingual array; extract the English text."""
    if isinstance(name_field, str):
        return name_field
    if isinstance(name_field, list):
        for entry in name_field:
            if isinstance(entry, dict) and entry.get("language") == "en":
                return entry.get("text", "")
        if name_field and isinstance(name_field[0], dict):
            return name_field[0].get("text", "")
    return ""


def fetch_all_stations() -> dict[str, dict[str, Any]]:
    """Fetch station metadata (name, lat, lon, capacity). Cached for the process lifetime."""
    global _info_cache
    if _info_cache is not None:
        return _info_cache
    with urlopen(STATION_INFO_URL, timeout=15) as response:
        feed = json.load(response)
    _info_cache = {
        s["station_id"]: {
            "station_id": s["station_id"],
            "name": _extract_name(s.get("name", "")),
            "lat": s.get("lat"),
            "lon": s.get("lon"),
            "capacity": s.get("capacity", 0),
        }
        for s in feed["data"]["stations"]
    }
    return _info_cache


def fetch_live_status() -> dict[str, dict[str, Any]]:
    """Fetch live dock counts for all stations. Cached for _STATUS_CACHE_TTL seconds."""
    global _status_cache
    now = time.monotonic()
    if _status_cache is not None and (now - _status_cache[0]) < _STATUS_CACHE_TTL:
        return _status_cache[1]
    with urlopen(STATION_STATUS_URL, timeout=15) as response:
        feed = json.load(response)
    data: dict[str, dict[str, Any]] = {
        s["station_id"]: {
            "station_id": s["station_id"],
            "num_docks_available": s.get("num_docks_available", 0),
            "num_bikes_available": s.get("num_bikes_available", 0),
            "is_returning": s.get("is_returning", 0),
            "station_status": s.get("station_status", "active"),
        }
        for s in feed["data"]["stations"]
    }
    _status_cache = (now, data)
    return data


def get_station_status(station_id: str) -> dict[str, Any]:
    """Merge live status and metadata for a single station."""
    info = fetch_all_stations().get(station_id, {})
    status = fetch_live_status().get(station_id, {})
    return {
        "station_id": station_id,
        "name": info.get("name", ""),
        "num_docks_available": status.get("num_docks_available", 0),
        "num_bikes_available": status.get("num_bikes_available", 0),
        "capacity": info.get("capacity", 0),
        "is_returning": status.get("is_returning", 0),
        "station_status": status.get("station_status", "unknown"),
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_nearby_stations(
    station_id: str,
    max_results: int = 5,
    max_radius_m: int = 800,
    min_docks: int = 1,
) -> list[dict[str, Any]]:
    """Nearest max_results stations that accept returns and have >= min_docks, capped at max_radius_m."""
    info = fetch_all_stations()
    status = fetch_live_status()

    anchor = info.get(station_id)
    if not anchor or anchor["lat"] is None or anchor["lon"] is None:
        return []

    candidates: list[dict[str, Any]] = []
    for sid, s_info in info.items():
        if sid == station_id:
            continue
        if s_info["lat"] is None or s_info["lon"] is None:
            continue
        s_status = status.get(sid, {})
        if s_status.get("is_returning", 0) == 0:
            continue
        docks = s_status.get("num_docks_available", 0)
        if docks < min_docks:
            continue
        dist = haversine_m(anchor["lat"], anchor["lon"], s_info["lat"], s_info["lon"])
        if dist > max_radius_m:
            continue
        candidates.append({
            "station_id": sid,
            "name": s_info["name"],
            "distance_m": round(dist),
            "docks_available": docks,
            "station_status": s_status.get("station_status", "active"),
        })

    candidates.sort(key=lambda x: x["distance_m"])
    return candidates[:max_results]


def main() -> None:
    all_info = fetch_all_stations()
    assert len(all_info) > 100, f"expected 100+ stations, got {len(all_info)}"
    sample = next(iter(all_info.values()))
    assert "lat" in sample and "lon" in sample and "capacity" in sample
    print(f"Station info: {len(all_info)} stations loaded")

    all_status = fetch_live_status()
    assert len(all_status) > 100, f"expected 100+ statuses, got {len(all_status)}"
    assert "num_docks_available" in next(iter(all_status.values()))
    print(f"Live status: {len(all_status)} stations fetched")

    sample_id = next(iter(all_info))
    station = get_station_status(sample_id)
    assert station["station_id"] == sample_id
    assert "num_docks_available" in station
    print(f"Sample station: {station['name']} - {station['num_docks_available']} docks available")

    nearby = get_nearby_stations(sample_id, max_radius_m=500, min_docks=1)
    print(f"Nearby stations within 500m of {all_info[sample_id]['name']}: {len(nearby)} found")
    for s in nearby[:3]:
        print(f"  {s['name']}: {s['docks_available']} docks, {s['distance_m']}m away")

    t0 = time.monotonic()
    fetch_live_status()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1, f"second call should hit cache, took {elapsed:.3f}s"
    print("TTL cache: ok")

    print("All assertions passed.")


if __name__ == "__main__":
    main()
