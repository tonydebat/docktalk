import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.bikeshare.trip_state import (
    make_initial_trip_state,
    record_dock_observation,
    record_tick_decision,
)

_ARRIVAL = datetime(2026, 6, 1, 9, 0, 0)


def _fresh() -> dict:
    return make_initial_trip_state("7202", "York St / Queen St W", _ARRIVAL)


# ── make_initial_trip_state ────────────────────────────────────────────────────

def test_initial_state_fields():
    state = _fresh()
    assert state["target_station_id"] == "7202"
    assert state["target_station_name"] == "York St / Queen St W"
    assert state["arrival_time"] == _ARRIVAL


def test_initial_state_defaults():
    state = _fresh()
    assert state["preferences"] == []
    assert state["dock_history"] == []
    assert state["recent_decisions"] == []
    assert state["rejected_station_ids"] == []
    assert state["target_just_switched"] is False
    assert state["status"] == "monitoring"


# ── record_dock_observation ────────────────────────────────────────────────────

def test_record_dock_observation_appends_entry():
    state = _fresh()
    record_dock_observation(state, docks_available=5, observed_at="2026-01-01T12:00:00Z")
    assert len(state["dock_history"]) == 1
    assert state["dock_history"][0]["docks_available"] == 5
    assert state["dock_history"][0]["observed_at"] == "2026-01-01T12:00:00Z"


def test_record_dock_observation_accumulates_across_calls():
    state = _fresh()
    record_dock_observation(state, docks_available=5, observed_at="t1")
    record_dock_observation(state, docks_available=3, observed_at="t2")
    record_dock_observation(state, docks_available=1, observed_at="t3")
    assert len(state["dock_history"]) == 3
    assert state["dock_history"][-1]["docks_available"] == 1


# ── record_tick_decision — set_next_check ──────────────────────────────────────

def test_record_tick_decision_set_next_check():
    state = _fresh()
    state["status"] = "monitoring"
    state["next_check_seconds"] = 60
    state["next_check_reason"] = "stable"
    record_tick_decision(state)
    assert len(state["recent_decisions"]) == 1
    d = state["recent_decisions"][0]
    assert d["action"] == "set_next_check"
    assert d["seconds"] == 60
    assert d["reason"] == "stable"


def test_record_tick_decision_accumulates_decisions():
    state = _fresh()
    state["status"] = "monitoring"
    state["next_check_seconds"] = 60
    state["next_check_reason"] = "first"
    record_tick_decision(state)
    state["next_check_seconds"] = 30
    state["next_check_reason"] = "second"
    record_tick_decision(state)
    assert len(state["recent_decisions"]) == 2


# ── record_tick_decision — alert_user ─────────────────────────────────────────

def test_record_tick_decision_alert_user():
    state = _fresh()
    state["status"] = "alerted"
    state["alert"] = {
        "headline": "Station is full",
        "message": "Try a nearby station.",
        "alternatives": [],
    }
    record_tick_decision(state)
    d = state["recent_decisions"][0]
    assert d["action"] == "alert_user"
    assert d["headline"] == "Station is full"


def test_record_tick_decision_alert_preferred_over_set_next_check():
    """When status is alerted, record alert_user even if next_check_seconds is also set."""
    state = _fresh()
    state["status"] = "alerted"
    state["alert"] = {"headline": "Low docks", "message": "Switch.", "alternatives": []}
    state["next_check_seconds"] = 20
    state["next_check_reason"] = "alert sent - checking soon"
    record_tick_decision(state)
    assert state["recent_decisions"][0]["action"] == "alert_user"


# ── record_tick_decision — finish_trip ────────────────────────────────────────

def test_record_tick_decision_finish_trip():
    state = _fresh()
    state["status"] = "finished"
    state["finish_reason"] = "arrival window passed"
    record_tick_decision(state)
    d = state["recent_decisions"][0]
    assert d["action"] == "finish_trip"
    assert d["reason"] == "arrival window passed"
