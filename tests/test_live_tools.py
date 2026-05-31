"""Unit tests for app/live_tools.py.

Focused on the tool handlers most likely to regress:
- handle_switch_station: dock_history seeding from available_docks arg
- handle_switch_station: alert cleared on switch
- handle_switch_station: dock_history empty when available_docks not provided
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.live_tools import (
    handle_get_ebike_stations_near_target,
    handle_get_nearby_ebike_stations,
    handle_switch_station,
)
from app.session_store import SessionRecord


def _make_record_with_active_station() -> SessionRecord:
    record = SessionRecord(session_id="test-live-1")
    record.trip_state = {
        "target_station_id": "7001",
        "target_station_name": "Union Station",
        "status": "monitoring",
        "dock_history": [{"observed_at": "2024-01-01T10:00:00", "docks_available": 12}],
        "recent_decisions": [],
        "rejected_station_ids": [],
        "alert": None,
    }
    record.status = "MONITORING_SAFE"
    return record


def test_switch_station_updates_target():
    record = _make_record_with_active_station()
    handle_switch_station({"station_id": "7002", "station_name": "King and Bay"}, record)
    assert record.trip_state["target_station_id"] == "7002"
    assert record.trip_state["target_station_name"] == "King and Bay"


def test_switch_station_seeds_dock_history_via_live_observation():
    """After switch, _seed_dock_observation fetches live GBFS data into dock_history."""
    record = _make_record_with_active_station()
    fake_status = {
        "observed_at": "2024-01-01T11:00:00",
        "num_docks_available": 23,
        "station_status": "active",
        "is_returning": 1,
    }
    with patch("app.live_tools.observe_target_station") as mock_observe:
        def fake_observe(trip_state):
            trip_state.setdefault("dock_history", []).append({
                "observed_at": fake_status["observed_at"],
                "docks_available": fake_status["num_docks_available"],
                "station_status": fake_status["station_status"],
                "is_returning": fake_status["is_returning"],
            })
            return fake_status
        mock_observe.side_effect = fake_observe
        handle_switch_station(
            {"station_id": "7002", "station_name": "King and Bay"},
            record,
        )
    mock_observe.assert_called_once()
    assert record.trip_state["dock_history"][-1]["docks_available"] == 23


def test_switch_station_calls_seed_dock_observation():
    """Even with no observation (e.g. network failure), dock_history is reset to empty."""
    record = _make_record_with_active_station()
    with patch("app.live_tools.observe_target_station", side_effect=Exception("network fail")):
        handle_switch_station({"station_id": "7002", "station_name": "King and Bay"}, record)
    # Pre-seed cleared; observe failed silently → empty history
    assert record.trip_state["dock_history"] == []


def test_switch_station_clears_stale_alert():
    record = _make_record_with_active_station()
    record.trip_state["alert"] = {
        "headline": "Union Station is filling up.",
        "message": "Only 1 dock left.",
        "alternatives": [],
    }
    record.trip_state["status"] = "alerted"
    with patch("app.live_tools.observe_target_station", side_effect=Exception("net")):
        handle_switch_station(
            {"station_id": "7002", "station_name": "King and Bay"},
            record,
        )
    assert record.trip_state["alert"] is None
    assert record.trip_state["status"] == "monitoring"


def test_switch_station_returns_switched_true():
    record = _make_record_with_active_station()
    with patch("app.live_tools.observe_target_station", side_effect=Exception("net")):
        result = handle_switch_station(
            {"station_id": "7002", "station_name": "King and Bay"},
            record,
        )
    assert result["switched"] is True
    assert result["station_id"] == "7002"
    assert result["station_name"] == "King and Bay"


# ── Jen's round-4 fixes ────────────────────────────────────────────────────────

def test_switch_station_status_reflects_real_dock_state():
    """After switch, record.status reflects derived status from real dock data, not hardcoded SAFE."""
    record = _make_record_with_active_station()
    low_dock_status = {
        "observed_at": "2024-01-01T11:00:00",
        "num_docks_available": 1,
        "station_status": "active",
        "is_returning": 1,
    }
    def fake_observe(trip_state):
        trip_state.setdefault("dock_history", []).append({
            "observed_at": low_dock_status["observed_at"],
            "docks_available": low_dock_status["num_docks_available"],
            "station_status": low_dock_status["station_status"],
            "is_returning": low_dock_status["is_returning"],
        })
        return low_dock_status
    with patch("app.live_tools.observe_target_station", side_effect=fake_observe):
        handle_switch_station({"station_id": "7002", "station_name": "King and Bay"}, record)
    assert record.status == "MONITORING_WARNING"


def test_begin_change_target_clears_stale_alert():
    """handle_begin_change_target must clear a stale alert so old options don't leak."""
    from app.live_tools import handle_begin_change_target
    record = _make_record_with_active_station()
    record.trip_state["alert"] = {"headline": "Union is full.", "message": "0 docks"}
    handle_begin_change_target({}, record)
    assert record.trip_state["alert"] is None


def test_format_status_payload_attaches_coordinates():
    """format_status_payload includes target_lat/lon and per-candidate/option lat/lon for the map."""
    from app.live_tools import format_status_payload
    record = _make_record_with_active_station()
    record.last_candidates = [{"station_id": "7002", "station_name": "King and Bay"}]
    record.last_options = [{"station_id": "7003", "station_name": "Front and Bay"}]
    stations_map = {
        "7001": {"name": "Union Station", "lat": 43.6453, "lon": -79.3806},
        "7002": {"name": "King and Bay", "lat": 43.6480, "lon": -79.3790},
        "7003": {"name": "Front and Bay", "lat": 43.6470, "lon": -79.3795},
    }
    with patch("app.live_tools.fetch_all_stations", return_value=stations_map):
        payload = format_status_payload(record)
    assert payload["target_lat"] == 43.6453
    assert payload["target_lon"] == -79.3806
    assert payload["candidates"][0]["lat"] == 43.6480
    assert payload["candidates"][0]["lon"] == -79.3790
    assert payload["options"][0]["lat"] == 43.6470
    assert payload["options"][0]["lon"] == -79.3795


def test_format_status_payload_includes_target_ebikes():
    from app.live_tools import format_status_payload
    record = _make_record_with_active_station()
    record.trip_state["dock_history"][-1]["ebikes_available"] = 3
    with patch("app.live_tools.fetch_all_stations", return_value={}):
        payload = format_status_payload(record)
    assert payload["ebikes"] == 3


def test_get_nearby_ebike_stations_requires_location():
    record = _make_record_with_active_station()
    record.rider_location = None

    result = handle_get_nearby_ebike_stations({}, record)

    assert result["location_available"] is False
    assert result["options"] == []
    assert "location" in result["spoken_message"].lower()


def test_get_nearby_ebike_stations_returns_top_options():
    record = _make_record_with_active_station()
    record.rider_location = {"lat": 43.65, "lon": -79.38}
    options = [
        {
            "station_id": "7002",
            "station_name": "King and Bay",
            "docks_available": 4,
            "ebikes_available": 2,
            "distance_m": 150,
            "battery_details_available": False,
        },
        {
            "station_id": "7003",
            "station_name": "Front and Bay",
            "docks_available": 3,
            "ebikes_available": 1,
            "distance_m": 240,
            "battery_details_available": False,
        },
    ]

    with patch("app.live_tools.get_nearby_ebike_stations", return_value=options) as mock_get:
        result = handle_get_nearby_ebike_stations({}, record)

    mock_get.assert_called_once_with(
        43.65,
        -79.38,
        max_results=3,
        exclude_station_id="7001",
    )
    assert result["location_available"] is True
    assert result["options"] == options
    assert record.last_options == options
    assert "e-bike" in result["spoken_message"]


def test_get_nearby_ebike_stations_before_target_does_not_exclude_station():
    record = SessionRecord(session_id="test-live-no-target")
    record.rider_location = {"lat": 43.65, "lon": -79.38}

    with patch("app.live_tools.get_nearby_ebike_stations", return_value=[]) as mock_get:
        handle_get_nearby_ebike_stations({}, record)

    mock_get.assert_called_once_with(
        43.65,
        -79.38,
        max_results=3,
        exclude_station_id=None,
    )


def test_get_ebike_stations_near_target_anchors_on_target_station():
    record = _make_record_with_active_station()
    stations_map = {
        "7001": {"name": "Union Station", "lat": 43.6453, "lon": -79.3806},
    }
    options = [
        {
            "station_id": "7004",
            "station_name": "Nearby E-bike Station",
            "docks_available": 8,
            "ebikes_available": 2,
            "distance_m": 220,
        },
    ]

    with (
        patch("app.live_tools.fetch_all_stations", return_value=stations_map),
        patch("app.live_tools.get_nearby_ebike_stations", return_value=options) as mock_get,
    ):
        result = handle_get_ebike_stations_near_target({}, record)

    mock_get.assert_called_once_with(
        43.6453,
        -79.3806,
        max_results=3,
        exclude_station_id="7001",
    )
    assert result["target_available"] is True
    assert result["options"] == options
    assert record.last_options == options
    assert "near Union Station" in result["spoken_message"]


def test_get_ebike_stations_near_target_requires_target():
    record = SessionRecord(session_id="test-live-no-target")

    result = handle_get_ebike_stations_near_target({}, record)

    assert result["target_available"] is False
    assert result["options"] == []
    assert "No target" in result["spoken_message"]


def test_switch_to_ebike_option_before_monitoring_starts_confirms_station():
    from app.live_tools import handle_switch_to_option

    record = SessionRecord(session_id="test-live-ebike-confirm")
    record.last_options = [
        {
            "station_id": "7002",
            "station_name": "Danforth Ave / Lamb Ave",
            "docks_available": 14,
            "ebikes_available": 1,
        },
    ]

    with patch("app.live_tools.observe_target_station", side_effect=Exception("offline")):
        result = handle_switch_to_option({"option_number": 1}, record)

    assert result["switched"] is True
    assert record.trip_state["target_station_id"] == "7002"
    assert record.trip_state["target_station_name"] == "Danforth Ave / Lamb Ave"
    assert record.spawn_monitor is True
    assert record.status == "MONITORING_SAFE"
