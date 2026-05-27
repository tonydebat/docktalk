"""Unit tests for ``geocode_to_nearby_stations``.

A fake geolocator is injected. We assert on the public list/dict shape
returned by the function, not on internal calls.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.bikeshare.geocoding import geocode_to_nearby_stations

# Union Station-ish anchor used by the FakeLocation below.
_ANCHOR_LAT = 43.6453
_ANCHOR_LON = -79.3806

# Five stations: three close, one just outside the radius, one offline.
_STATIONS = {
    "near-1": {
        "station_id": "near-1",
        "name": "Front and Bay",
        "lat": 43.6455,  # ~30 m away
        "lon": -79.3800,
        "num_docks_available": 5,
        "station_status": "active",
    },
    "near-2": {
        "station_id": "near-2",
        "name": "Wellington and York",
        "lat": 43.6470,  # ~250 m away
        "lon": -79.3815,
        "num_docks_available": 3,
        "station_status": "active",
    },
    "near-3": {
        "station_id": "near-3",
        "name": "King and Bay",
        "lat": 43.6485,  # ~400 m away
        "lon": -79.3795,
        "num_docks_available": 2,
        "station_status": "active",
    },
    "outside": {
        "station_id": "outside",
        "name": "Far Far Away",
        "lat": 43.7000,  # ~6 km away — well outside 1500 m
        "lon": -79.3806,
        "num_docks_available": 8,
        "station_status": "active",
    },
    "offline": {
        "station_id": "offline",
        "name": "Closed Station",
        "lat": 43.6454,  # ~10 m away but offline
        "lon": -79.3807,
        "num_docks_available": 10,
        "station_status": "out_of_service",
    },
}


class _FakeLocation:
    def __init__(self, lat: float, lon: float, *, address: str, raw: dict | None = None) -> None:
        self.latitude = lat
        self.longitude = lon
        self.address = address
        self.raw = raw or {"address": {"attraction": address.split(",", 1)[0]}}


class _FakeGeolocator:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def geocode(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def test_hit_inside_radius_returns_ranked_nearby_stations():
    fake = _FakeGeolocator(
        _FakeLocation(_ANCHOR_LAT, _ANCHOR_LON, address="Union Station, Toronto"),
    )
    results = geocode_to_nearby_stations(
        "Union Station", _STATIONS, geolocator=fake
    )
    ids = [r["station_id"] for r in results]
    assert ids == ["near-1", "near-2", "near-3"]
    # Recommendation-object shape:
    first = results[0]
    assert set(first.keys()) >= {
        "station_id",
        "name",
        "location_hint",
        "available_docks",
        "distance_meters",
        "station_status",
        "recommendation_reason",
    }
    assert first["recommendation_reason"].startswith("closest dock to ")
    assert first["distance_meters"] >= 0


def test_hit_outside_radius_returns_empty_list():
    # Geocode hit but in Markham — no stations within 1500 m.
    markham = _FakeGeolocator(
        _FakeLocation(43.8561, -79.3370, address="Markham, ON"),
    )
    assert geocode_to_nearby_stations("Markham", _STATIONS, geolocator=markham) == []


def test_geocoder_miss_returns_empty_list():
    fake = _FakeGeolocator(None)
    assert geocode_to_nearby_stations("gibberish", _STATIONS, geolocator=fake) == []


def test_geocoder_error_returns_empty_list():
    fake = _FakeGeolocator(RuntimeError("Nominatim timeout"))
    assert geocode_to_nearby_stations("anything", _STATIONS, geolocator=fake) == []


def test_offline_and_zero_dock_stations_are_filtered():
    # The offline station is 10 m away — it must NOT appear even though it's
    # the closest candidate.
    fake = _FakeGeolocator(
        _FakeLocation(_ANCHOR_LAT, _ANCHOR_LON, address="Union Station"),
    )
    results = geocode_to_nearby_stations("Union Station", _STATIONS, geolocator=fake)
    assert "offline" not in [r["station_id"] for r in results]


def test_zero_dock_station_is_filtered():
    stations = {
        "z": {
            "station_id": "z",
            "name": "Zero Docks",
            "lat": _ANCHOR_LAT,
            "lon": _ANCHOR_LON,
            "num_docks_available": 0,
            "station_status": "active",
        }
    }
    fake = _FakeGeolocator(
        _FakeLocation(_ANCHOR_LAT, _ANCHOR_LON, address="Anywhere"),
    )
    assert geocode_to_nearby_stations("anywhere", stations, geolocator=fake) == []


def test_k_parameter_caps_results():
    fake = _FakeGeolocator(
        _FakeLocation(_ANCHOR_LAT, _ANCHOR_LON, address="Union Station"),
    )
    results = geocode_to_nearby_stations(
        "Union Station", _STATIONS, geolocator=fake, k=2
    )
    assert len(results) == 2


def test_radius_m_parameter_widens_search():
    fake = _FakeGeolocator(
        _FakeLocation(_ANCHOR_LAT, _ANCHOR_LON, address="Union Station"),
    )
    # 10 km radius should pull the "outside" station in too.
    results = geocode_to_nearby_stations(
        "Union Station", _STATIONS, geolocator=fake, radius_m=10_000
    )
    assert "outside" in [r["station_id"] for r in results]


def test_empty_transcript_returns_empty_list():
    fake = _FakeGeolocator(_FakeLocation(0, 0, address="x"))
    assert geocode_to_nearby_stations("", _STATIONS, geolocator=fake) == []
    assert geocode_to_nearby_stations("   ", _STATIONS, geolocator=fake) == []


def test_place_name_falls_back_to_address_prefix():
    fake = _FakeGeolocator(
        _FakeLocation(_ANCHOR_LAT, _ANCHOR_LON, address="Union Station, Toronto, ON", raw={}),
    )
    results = geocode_to_nearby_stations("Union", _STATIONS, geolocator=fake)
    assert results
    assert results[0]["recommendation_reason"] == "closest dock to Union Station"
