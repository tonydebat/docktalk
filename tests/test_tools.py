"""Unit tests for app/tools.py conversation tool handlers.

All network I/O is injected/mocked so tests run offline.
Tests focus on the public contract: correct return shapes, correct filtering,
and correct trip state mutations.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.session_store import SessionRecord
from app.tools import (
    dispatch,
    handle_confirm_station,
    handle_get_backup_options,
    handle_get_station_status,
    handle_resolve_destination,
    handle_stop_monitoring,
    handle_switch_station,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _make_record(session_id="test-session") -> SessionRecord:
    return SessionRecord(session_id=session_id)


def _make_record_with_state(session_id="test-session") -> SessionRecord:
    r = _make_record(session_id)
    r.trip_state = {
        "target_station_id": "station_1",
        "target_station_name": "Union Station",
        "status": "monitoring",
        "dock_history": [{"docks_available": 4, "observed_at": "2025-01-01T12:00:00"}],
        "rejected_station_ids": [],
    }
    return r


_CANDIDATES = [
    {
        "station_id": "station_1",
        "name": "Union Station",
        "location_hint": "near Front and Bay",
        "available_docks": 4,
        "distance_meters": 150,
        "station_status": "active",
        "recommendation_reason": "closest active station",
    }
]


# ── resolve_destination ───────────────────────────────────────────────────────


def test_resolve_destination_returns_candidates():
    record = _make_record()
    with (
        patch("app.tools.fetch_all_stations", return_value={}),
        patch("app.tools.fetch_live_status", return_value={}),
        patch(
            "app.tools._resolver.merge_info_and_status",
            return_value={"station_1": {}},
        ),
        patch(
            "app.tools._resolver.resolve_destination",
            return_value=_CANDIDATES,
        ),
    ):
        result = handle_resolve_destination({"transcript": "near union station"}, record)

    assert "candidates" in result
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["station_id"] == "station_1"


def test_resolve_destination_empty_returns_message():
    record = _make_record()
    with (
        patch("app.tools.fetch_all_stations", return_value={}),
        patch("app.tools.fetch_live_status", return_value={}),
        patch("app.tools._resolver.merge_info_and_status", return_value={}),
        patch("app.tools._resolver.resolve_destination", return_value=[]),
    ):
        result = handle_resolve_destination({"transcript": "somewhere unknown"}, record)

    assert result["candidates"] == []
    assert "message" in result


# ── confirm_station ───────────────────────────────────────────────────────────


def test_confirm_station_creates_trip_state():
    record = _make_record()
    result = handle_confirm_station(
        {"station_id": "station_1", "station_name": "Union Station"}, record
    )

    assert result["confirmed"] is True
    assert record.trip_state is not None
    assert record.trip_state["target_station_id"] == "station_1"
    assert record.trip_state["target_station_name"] == "Union Station"
    assert record.spawn_monitor is True


def test_confirm_station_sets_monitoring_status():
    record = _make_record()
    handle_confirm_station(
        {"station_id": "station_1", "station_name": "Union Station"}, record
    )
    assert record.status == "MONITORING_SAFE"


# ── get_station_status ────────────────────────────────────────────────────────


def test_get_station_status_returns_required_fields():
    record = _make_record()
    mock_status = {
        "name": "Union Station",
        "num_docks_available": 7,
        "station_status": "active",
        "observed_at": "2025-01-01T12:00:00",
    }
    with patch("app.tools.get_station_status", return_value=mock_status):
        result = handle_get_station_status({"station_id": "station_1"}, record)

    assert result["station_id"] == "station_1"
    assert result["available_docks"] == 7
    assert result["station_status"] == "active"


# ── get_backup_options ────────────────────────────────────────────────────────


def _make_nearby_stations():
    return [
        {  # same as target — must be excluded
            "station_id": "station_1",
            "name": "Union Station",
            "docks_available": 4,
            "distance_m": 0,
            "station_status": "active",
        },
        {  # offline — must be excluded
            "station_id": "station_2",
            "name": "Bay and Front",
            "docks_available": 5,
            "distance_m": 200,
            "station_status": "offline",
        },
        {  # zero docks — must be excluded
            "station_id": "station_3",
            "name": "Simcoe and Front",
            "docks_available": 0,
            "distance_m": 300,
            "station_status": "active",
        },
        {  # valid backup
            "station_id": "station_4",
            "name": "Wellington and York",
            "docks_available": 6,
            "distance_m": 350,
            "station_status": "active",
        },
    ]


def test_get_backup_options_excludes_target():
    record = _make_record()
    with patch("app.tools.get_nearby_stations", return_value=_make_nearby_stations()):
        result = handle_get_backup_options({"station_id": "station_1"}, record)

    ids = [o["station_id"] for o in result["options"]]
    assert "station_1" not in ids


def test_get_backup_options_excludes_offline():
    record = _make_record()
    with patch("app.tools.get_nearby_stations", return_value=_make_nearby_stations()):
        result = handle_get_backup_options({"station_id": "station_1"}, record)

    ids = [o["station_id"] for o in result["options"]]
    assert "station_2" not in ids


def test_get_backup_options_excludes_zero_docks():
    record = _make_record()
    with patch("app.tools.get_nearby_stations", return_value=_make_nearby_stations()):
        result = handle_get_backup_options({"station_id": "station_1"}, record)

    ids = [o["station_id"] for o in result["options"]]
    assert "station_3" not in ids


def test_get_backup_options_max_three():
    # Build 5 valid backups
    many = [
        {"station_id": f"s{i}", "name": f"Station {i}", "docks_available": 3,
         "distance_m": i * 100, "station_status": "active"}
        for i in range(2, 8)
    ]
    record = _make_record()
    with patch("app.tools.get_nearby_stations", return_value=many):
        result = handle_get_backup_options({"station_id": "station_1"}, record)

    assert len(result["options"]) <= 3


# ── switch_station ────────────────────────────────────────────────────────────


def test_switch_station_updates_trip_state():
    record = _make_record_with_state()
    result = handle_switch_station(
        {"station_id": "station_4", "station_name": "Wellington and York"}, record
    )

    assert result["switched"] is True
    assert record.trip_state["target_station_id"] == "station_4"
    assert record.trip_state["target_station_name"] == "Wellington and York"
    assert record.trip_state["target_just_switched"] is True
    # Old target should be in rejected list
    assert "station_1" in record.trip_state["rejected_station_ids"]


# ── stop_monitoring ───────────────────────────────────────────────────────────


def test_stop_monitoring_sets_stopped_status():
    record = _make_record_with_state()
    result = handle_stop_monitoring({"reason": "rider returned bike"}, record)

    assert result["stopped"] is True
    assert record.status == "STOPPED"
    assert record.trip_state["status"] == "finished"


def test_stop_monitoring_no_trip_state():
    record = _make_record()
    result = handle_stop_monitoring({"reason": "cancelled"}, record)
    assert result["stopped"] is True
    assert record.status == "STOPPED"


# ── dispatch (async) ──────────────────────────────────────────────────────────


def test_dispatch_unknown_tool_returns_error():
    record = _make_record()
    result = asyncio.run(dispatch("nonexistent_tool", {}, record))
    assert "error" in result


def test_dispatch_routes_to_handler():
    record = _make_record()
    with (
        patch("app.tools.fetch_all_stations", return_value={}),
        patch("app.tools.fetch_live_status", return_value={}),
        patch("app.tools._resolver.merge_info_and_status", return_value={}),
        patch("app.tools._resolver.resolve_destination", return_value=_CANDIDATES),
    ):
        result = asyncio.run(
            dispatch("resolve_destination", {"transcript": "union"}, record)
        )
    assert "candidates" in result
