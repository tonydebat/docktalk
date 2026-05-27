import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.bikeshare.station_search import search_stations

_STATIONS = {
    "1": {"station_id": "1", "name": "Union Station", "lat": 43.645, "lon": -79.380, "capacity": 20},
    "2": {"station_id": "2", "name": "Bay St / Union Station", "lat": 43.644, "lon": -79.381, "capacity": 15},
    "3": {"station_id": "3", "name": "King St W / Bay St", "lat": 43.648, "lon": -79.382, "capacity": 10},
    "4": {"station_id": "4", "name": "Queens Quay W / Rees St", "lat": 43.638, "lon": -79.385, "capacity": 25},
    "5": {"station_id": "5", "name": "Spadina Ave / King St W", "lat": 43.645, "lon": -79.395, "capacity": 18},
}


def test_exact_name_match():
    results = search_stations("Union Station", _STATIONS)
    ids = [r["station_id"] for r in results]
    assert "1" in ids
    assert "2" in ids


def test_case_insensitive_match():
    lower = search_stations("union station", _STATIONS)
    upper = search_stations("UNION STATION", _STATIONS)
    assert [r["station_id"] for r in lower] == [r["station_id"] for r in upper]
    assert any(r["station_id"] == "1" for r in lower)


def test_no_match_returns_empty():
    results = search_stations("zzzzzzznomatch", _STATIONS)
    assert results == []


def test_empty_query_returns_empty():
    results = search_stations("", _STATIONS)
    assert results == []


def test_ranked_by_match_position():
    # "Union Station" starts at pos 0; "Bay St / Union Station" has "union" at pos 9
    results = search_stations("union", _STATIONS)
    ids = [r["station_id"] for r in results]
    assert ids.index("1") < ids.index("2")


def test_query_longer_than_all_names():
    results = search_stations("x" * 1000, _STATIONS)
    assert results == []


def test_result_includes_station_id():
    results = search_stations("Union", _STATIONS)
    for r in results:
        assert "station_id" in r


def test_max_ten_results():
    many = {
        str(i): {"station_id": str(i), "name": f"Test Station {i}", "lat": 0.0, "lon": 0.0, "capacity": 10}
        for i in range(25)
    }
    results = search_stations("station", many)
    assert len(results) <= 10


def test_partial_match():
    results = search_stations("Bay", _STATIONS)
    ids = [r["station_id"] for r in results]
    assert "2" in ids  # "Bay St / Union Station"
    assert "3" in ids  # "King St W / Bay St"
