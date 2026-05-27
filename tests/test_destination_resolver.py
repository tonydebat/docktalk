"""Unit tests for ``resolve_destination``.

The three dependencies (parse_intent, geocode, search) are injected so
we never make real network calls. Each test drives a specific cascade
path and asserts on the public return shape.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.bikeshare.destination_resolver import resolve_destination

_STATIONS = {
    "1": {
        "station_id": "1",
        "name": "Union Station",
        "lat": 43.645,
        "lon": -79.380,
        "num_docks_available": 4,
        "station_status": "active",
    },
    "2": {
        "station_id": "2",
        "name": "Front and Bay",
        "lat": 43.646,
        "lon": -79.381,
        "num_docks_available": 7,
        "station_status": "active",
    },
}


def _make_search(hits_by_term: dict[str, list[dict]]):
    def search(term, _stations):
        return hits_by_term.get(term, [])
    return search


def _make_geocode(result):
    def geocode(_transcript, _stations, **_kwargs):
        return result
    return geocode


def test_step1_first_term_hit():
    parse = lambda _t: ["Union Station", "Front and Bay"]
    search = _make_search({
        "Union Station": [{"station_id": "1", "name": "Union Station", "num_docks_available": 4, "station_status": "active"}],
    })
    geocode_called: list[bool] = []
    geocode = lambda *a, **k: (geocode_called.append(True) or [])

    results = resolve_destination(
        "near Union Station", _STATIONS,
        parse_intent=parse, geocode=geocode, search=search,
    )
    assert len(results) == 1
    assert results[0]["station_id"] == "1"
    assert results[0]["recommendation_reason"] == "name match for 'Union Station'"
    # Geocode must not be called when Step 1 succeeds.
    assert geocode_called == []


def test_step1_falls_through_to_later_term():
    parse = lambda _t: ["Wrong Term", "Front and Bay"]
    search = _make_search({
        "Front and Bay": [{"station_id": "2", "name": "Front and Bay", "num_docks_available": 7, "station_status": "active"}],
    })
    geocode = _make_geocode([])

    results = resolve_destination(
        "Front and Bay area", _STATIONS,
        parse_intent=parse, geocode=geocode, search=search,
    )
    assert len(results) == 1
    assert results[0]["station_id"] == "2"
    assert results[0]["recommendation_reason"] == "name match for 'Front and Bay'"


def test_step1_miss_step2_hit():
    parse = lambda _t: ["Made Up Place"]
    search = _make_search({})  # no name matches at all
    geocoded = [{
        "station_id": "2",
        "name": "Front and Bay",
        "location_hint": "Front and Bay",
        "available_docks": 7,
        "distance_meters": 120,
        "station_status": "active",
        "recommendation_reason": "closest dock to CN Tower",
    }]
    geocode = _make_geocode(geocoded)

    results = resolve_destination(
        "near the CN Tower", _STATIONS,
        parse_intent=parse, geocode=geocode, search=search,
    )
    assert results == geocoded


def test_step1_miss_step2_miss_returns_empty():
    parse = lambda _t: ["Nope"]
    search = _make_search({})
    geocode = _make_geocode([])

    assert resolve_destination(
        "gibberish",
        _STATIONS,
        parse_intent=parse, geocode=geocode, search=search,
    ) == []


def test_geocode_receives_raw_transcript_not_parsed_query():
    parse = lambda _t: ["Some Different Thing"]
    search = _make_search({})
    captured: dict[str, str] = {}

    def geocode(transcript, _stations, **_kwargs):
        captured["transcript"] = transcript
        return []

    resolve_destination(
        "  Original rider words  ",
        _STATIONS,
        parse_intent=parse, geocode=geocode, search=search,
    )
    # The cascade strips whitespace but does NOT substitute Gemini's reformulation.
    assert captured["transcript"] == "Original rider words"


def test_empty_transcript_returns_empty_list():
    parse = lambda _t: ["should not be called"]
    search = _make_search({})
    geocode = _make_geocode([])

    assert resolve_destination("", _STATIONS, parse_intent=parse, geocode=geocode, search=search) == []
    assert resolve_destination("   ", _STATIONS, parse_intent=parse, geocode=geocode, search=search) == []


def test_step1_no_terms_falls_through_to_geocode():
    parse = lambda _t: []  # Gemini failure
    search = _make_search({})
    geocoded = [{
        "station_id": "2",
        "name": "Front and Bay",
        "location_hint": "Front and Bay",
        "available_docks": 7,
        "distance_meters": 200,
        "station_status": "active",
        "recommendation_reason": "closest dock to Front Street",
    }]
    geocode = _make_geocode(geocoded)

    results = resolve_destination(
        "anything",
        _STATIONS,
        parse_intent=parse, geocode=geocode, search=search,
    )
    assert results == geocoded


def test_results_capped_at_five():
    parse = lambda _t: ["x"]
    big_hits = [
        {"station_id": str(i), "name": f"S{i}", "num_docks_available": 5, "station_status": "active"}
        for i in range(10)
    ]
    search = _make_search({"x": big_hits})
    geocode = _make_geocode([])

    results = resolve_destination(
        "x", _STATIONS, parse_intent=parse, geocode=geocode, search=search,
    )
    assert len(results) == 5
