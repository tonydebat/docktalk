"""Unit tests for app/live_tools.py.

Focused on the tool handlers most likely to regress:
- handle_switch_station: dock_history seeding from available_docks arg
- handle_switch_station: alert cleared on switch
- handle_switch_station: dock_history empty when available_docks not provided
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.live_tools import handle_switch_station
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


def test_switch_station_seeds_dock_history_when_docks_provided():
    record = _make_record_with_active_station()
    handle_switch_station(
        {"station_id": "7002", "station_name": "King and Bay", "available_docks": 23},
        record,
    )
    assert len(record.trip_state["dock_history"]) == 1
    assert record.trip_state["dock_history"][0]["docks_available"] == 23


def test_switch_station_clears_dock_history_when_docks_not_provided():
    record = _make_record_with_active_station()
    handle_switch_station({"station_id": "7002", "station_name": "King and Bay"}, record)
    assert record.trip_state["dock_history"] == []


def test_switch_station_clears_stale_alert():
    record = _make_record_with_active_station()
    record.trip_state["alert"] = {
        "headline": "Union Station is filling up.",
        "message": "Only 1 dock left.",
        "alternatives": [],
    }
    record.trip_state["status"] = "alerted"
    handle_switch_station(
        {"station_id": "7002", "station_name": "King and Bay", "available_docks": 8},
        record,
    )
    assert record.trip_state["alert"] is None
    assert record.trip_state["status"] == "monitoring"


def test_switch_station_returns_switched_true():
    record = _make_record_with_active_station()
    result = handle_switch_station(
        {"station_id": "7002", "station_name": "King and Bay"},
        record,
    )
    assert result["switched"] is True
    assert result["station_id"] == "7002"
    assert result["station_name"] == "King and Bay"
