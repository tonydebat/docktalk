"""Offline tests for new agent.py functions.

Tests cover:
- handle_rider_command: all intents including unknown
- apply_alert_response: switch, keep, cancel, unknown, out-of-range index
- apply_lifecycle_checks: paused states, check_in trigger after grace period
- observe_target_station: appends to dock_history, sets latest_station_status
- run_monitor_tick: lifecycle short-circuit, normal path calls observe + run_tick
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.bikeshare.agent import (
    apply_alert_response,
    apply_lifecycle_checks,
    handle_rider_command,
    observe_target_station,
    run_monitor_tick,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_trip_state(**overrides) -> dict:
    state = {
        "target_station_id": "s1",
        "target_station_name": "Union Station",
        "arrival_time": datetime.now() + timedelta(minutes=10),
        "dock_history": [],
        "recent_decisions": [],
        "rejected_station_ids": [],
        "status": "monitoring",
        "alert": None,
    }
    state.update(overrides)
    return state


def _make_station_status(docks=5, station_status="active", is_returning=1) -> dict:
    return {
        "num_docks_available": docks,
        "station_status": station_status,
        "is_returning": is_returning,
        "observed_at": datetime.now().isoformat(),
        "name": "Union Station",
    }


def _make_alert(alternatives=None) -> dict:
    if alternatives is None:
        alternatives = [
            {"station_id": "s2", "station_name": "Bay and Front", "docks_available": 7},
            {"station_id": "s3", "station_name": "Wellington and York", "docks_available": 5},
        ]
    return {"headline": "Low docks", "message": "Switch?", "alternatives": alternatives}


# ── handle_rider_command ──────────────────────────────────────────────────────

class TestHandleRiderCommand:
    def test_get_update_returns_station_message(self):
        trip_state = _make_trip_state()
        with patch("src.bikeshare.agent.observe_target_station", return_value=_make_station_status(5)) as mock_obs:
            result = handle_rider_command({"intent": "get_update"}, trip_state)
        assert result["action"] == "get_update"
        assert result["source"] == "rider_command"
        assert "5" in result["message"]
        mock_obs.assert_called_once()

    def test_get_update_alias_update(self):
        trip_state = _make_trip_state()
        with patch("src.bikeshare.agent.observe_target_station", return_value=_make_station_status(3)):
            result = handle_rider_command({"intent": "update"}, trip_state)
        assert result["action"] == "get_update"

    def test_show_options_returns_options(self):
        trip_state = _make_trip_state()
        fake_options = [
            {"station_name": "Bay and Front", "docks_available": 7, "walking_minutes": 3},
        ]
        with patch("src.bikeshare.agent._safe_get_nearby_stations", return_value=fake_options):
            result = handle_rider_command({"intent": "show_options"}, trip_state)
        assert result["action"] == "show_options"
        assert result["options"] == fake_options
        assert "Bay and Front" in result["message"]

    def test_show_options_empty_nearby(self):
        trip_state = _make_trip_state()
        with patch("src.bikeshare.agent._safe_get_nearby_stations", return_value=[]):
            result = handle_rider_command({"intent": "show_options"}, trip_state)
        assert result["action"] == "show_options"
        assert "not see a good" in result["message"]

    def test_cancel_monitoring_sets_finished(self):
        trip_state = _make_trip_state()
        result = handle_rider_command({"intent": "cancel_monitoring"}, trip_state)
        assert result["action"] == "cancel_monitoring"
        assert trip_state["status"] == "finished"
        assert "stopped" in result["message"].lower()

    def test_finish_trip_sets_finished(self):
        trip_state = _make_trip_state()
        result = handle_rider_command({"intent": "finish_trip"}, trip_state)
        assert result["action"] == "finish_trip"
        assert trip_state["status"] == "finished"

    def test_change_target_requests_change(self):
        trip_state = _make_trip_state()
        result = handle_rider_command({"intent": "change_target"}, trip_state)
        assert result["action"] == "change_target"
        assert trip_state.get("change_target_requested") is True
        assert trip_state["status"] == "finished"

    def test_unknown_intent_returns_unknown(self):
        trip_state = _make_trip_state()
        result = handle_rider_command({"intent": "do_something_weird"}, trip_state)
        assert result["action"] == "unknown"
        assert result["source"] == "rider_command"

    def test_appends_to_recent_decisions(self):
        trip_state = _make_trip_state()
        with patch("src.bikeshare.agent.observe_target_station", return_value=_make_station_status(5)):
            handle_rider_command({"intent": "get_update"}, trip_state)
        assert len(trip_state["recent_decisions"]) == 1
        assert trip_state["recent_decisions"][0]["action"] == "get_update"


# ── apply_alert_response ──────────────────────────────────────────────────────

class TestApplyAlertResponse:
    def test_switch_station_changes_target(self):
        trip_state = _make_trip_state(alert=_make_alert(), status="alerted")
        result = apply_alert_response({"intent": "switch_station", "alternative_index": 0}, trip_state)
        assert result["action"] == "switch_station"
        assert trip_state["target_station_id"] == "s2"
        assert trip_state["target_station_name"] == "Bay and Front"
        assert trip_state["status"] == "monitoring"
        assert "s1" in trip_state["rejected_station_ids"]

    def test_switch_station_second_alternative(self):
        trip_state = _make_trip_state(alert=_make_alert(), status="alerted")
        result = apply_alert_response({"intent": "switch_station", "alternative_index": 1}, trip_state)
        assert result["action"] == "switch_station"
        assert trip_state["target_station_id"] == "s3"

    def test_switch_station_out_of_range_index(self):
        trip_state = _make_trip_state(alert=_make_alert(), status="alerted")
        result = apply_alert_response({"intent": "switch_station", "alternative_index": 99}, trip_state)
        assert result["action"] == "error"
        assert trip_state["target_station_id"] == "s1"  # unchanged

    def test_switch_station_no_alternatives(self):
        trip_state = _make_trip_state(alert=_make_alert(alternatives=[]), status="alerted")
        result = apply_alert_response({"intent": "switch_station"}, trip_state)
        assert result["action"] == "error"

    def test_keep_target_clears_alert(self):
        trip_state = _make_trip_state(alert=_make_alert(), status="alerted")
        result = apply_alert_response({"intent": "keep_target"}, trip_state)
        assert result["action"] == "keep_target"
        assert trip_state["alert"] is None
        assert trip_state["status"] == "monitoring"

    def test_cancel_monitoring_stops(self):
        trip_state = _make_trip_state(alert=_make_alert(), status="alerted")
        result = apply_alert_response({"intent": "cancel_monitoring"}, trip_state)
        assert result["action"] == "cancel_monitoring"
        assert trip_state["status"] == "finished"

    def test_unknown_intent_returns_unknown(self):
        trip_state = _make_trip_state(alert=_make_alert(), status="alerted")
        result = apply_alert_response({"intent": "something_else"}, trip_state)
        assert result["action"] == "unknown"

    def test_switch_sets_next_check_at(self):
        trip_state = _make_trip_state(alert=_make_alert(), status="alerted")
        now = datetime.now()
        result = apply_alert_response({"intent": "switch_station", "alternative_index": 0}, trip_state, now=now)
        assert "next_check_at" in trip_state
        assert trip_state["next_check_at"] > now


# ── apply_lifecycle_checks ────────────────────────────────────────────────────

class TestApplyLifecycleChecks:
    @pytest.mark.parametrize("paused_status", ["alerted", "check_in", "finished"])
    def test_paused_status_skips(self, paused_status):
        trip_state = _make_trip_state(status=paused_status)
        result = apply_lifecycle_checks(trip_state)
        assert result is not None
        assert result["action"] == "skip"
        assert result["source"] == "lifecycle"

    def test_monitoring_status_returns_none(self):
        trip_state = _make_trip_state(status="monitoring")
        result = apply_lifecycle_checks(trip_state)
        assert result is None

    def test_check_in_triggered_after_grace_period(self):
        grace_minutes_ago = timedelta(minutes=31)
        past_arrival = datetime.now() - grace_minutes_ago
        trip_state = _make_trip_state(status="monitoring", arrival_time=past_arrival)
        result = apply_lifecycle_checks(trip_state)
        assert result is not None
        assert result["action"] == "check_in"
        assert trip_state["status"] == "check_in"
        assert "check_in" in trip_state

    def test_check_in_not_triggered_before_grace_period(self):
        trip_state = _make_trip_state(
            status="monitoring",
            arrival_time=datetime.now() + timedelta(minutes=5),
        )
        result = apply_lifecycle_checks(trip_state)
        assert result is None

    def test_arrival_just_passed_no_check_in_yet(self):
        trip_state = _make_trip_state(
            status="monitoring",
            arrival_time=datetime.now() - timedelta(minutes=1),
        )
        result = apply_lifecycle_checks(trip_state)
        assert result is None  # within grace period


# ── observe_target_station ────────────────────────────────────────────────────

class TestObserveTargetStation:
    def test_appends_to_dock_history(self):
        trip_state = _make_trip_state()
        fake_status = _make_station_status(docks=6)
        with patch("src.bikeshare.agent.agent_tools.get_station_status", return_value=fake_status):
            observe_target_station(trip_state)
        assert len(trip_state["dock_history"]) == 1
        assert trip_state["dock_history"][0]["docks_available"] == 6

    def test_sets_latest_station_status(self):
        trip_state = _make_trip_state()
        fake_status = _make_station_status(docks=3)
        with patch("src.bikeshare.agent.agent_tools.get_station_status", return_value=fake_status):
            result = observe_target_station(trip_state)
        assert result is fake_status
        assert trip_state["latest_station_status"] is fake_status

    def test_caps_dock_history_at_max(self):
        from src.bikeshare.agent import MAX_DOCK_HISTORY
        trip_state = _make_trip_state(
            dock_history=[
                {"observed_at": f"t-{i}", "docks_available": i}
                for i in range(MAX_DOCK_HISTORY)
            ]
        )
        fake_status = _make_station_status(docks=99)
        with patch("src.bikeshare.agent.agent_tools.get_station_status", return_value=fake_status):
            observe_target_station(trip_state)
        assert len(trip_state["dock_history"]) == MAX_DOCK_HISTORY
        assert trip_state["dock_history"][-1]["docks_available"] == 99


# ── run_monitor_tick ──────────────────────────────────────────────────────────

class TestRunMonitorTick:
    def test_lifecycle_short_circuit_on_paused(self):
        trip_state = _make_trip_state(status="finished")
        with patch("src.bikeshare.agent.run_tick") as mock_tick:
            result = run_monitor_tick(trip_state)
        assert result["action"] == "skip"
        mock_tick.assert_not_called()

    def test_calls_observe_then_run_tick_when_monitoring(self):
        trip_state = _make_trip_state(status="monitoring")
        fake_status = _make_station_status(docks=4)
        expected_result = {"source": "fallback", "trace": [], "trip_state": trip_state}

        with (
            patch("src.bikeshare.agent.agent_tools.get_station_status", return_value=fake_status),
            patch("src.bikeshare.agent.run_tick", return_value=expected_result) as mock_tick,
        ):
            result = run_monitor_tick(trip_state)

        assert result is expected_result
        mock_tick.assert_called_once_with(trip_state)
        assert trip_state["latest_station_status"] is fake_status


# ── _switch_to_option ─────────────────────────────────────────────────────────

class TestSwitchToOption:
    def _make_options(self):
        return [
            {"station_id": "s2", "station_name": "Bay and Front", "docks_available": 7},
            {"station_id": "s3", "station_name": "Wellington and York", "docks_available": 5},
        ]

    def test_success_switches_station(self):
        from src.bikeshare.agent import _switch_to_option
        trip_state = _make_trip_state(last_options=self._make_options())
        command = {"alternative_index": 0}
        result = _switch_to_option(command, trip_state, options_key="last_options", reason="test")
        assert result["action"] == "switch_station"
        assert trip_state["target_station_id"] == "s2"
        assert trip_state["target_station_name"] == "Bay and Front"

    def test_success_seeds_dock_history_from_chosen(self):
        from src.bikeshare.agent import _switch_to_option
        trip_state = _make_trip_state(
            last_options=self._make_options(),
            dock_history=[{"docks_available": 1}],
            alert={"headline": "Full"},
        )
        command = {"alternative_index": 1}  # Wellington and York, docks_available=5
        _switch_to_option(command, trip_state, options_key="last_options", reason="test")
        assert len(trip_state["dock_history"]) == 1
        assert trip_state["dock_history"][0]["docks_available"] == 5
        assert trip_state["alert"] is None
        assert trip_state["status"] == "monitoring"
        assert trip_state["next_check_seconds"] == 20

    def test_success_empty_dock_history_when_docks_unknown(self):
        from src.bikeshare.agent import _switch_to_option
        options_no_docks = [{"station_id": "s2", "station_name": "Bay and Front"}]
        trip_state = _make_trip_state(last_options=options_no_docks)
        _switch_to_option({"alternative_index": 0}, trip_state, options_key="last_options", reason="test")
        assert trip_state["dock_history"] == []

    def test_success_appends_rejected_station(self):
        from src.bikeshare.agent import _switch_to_option
        trip_state = _make_trip_state(last_options=self._make_options())
        _switch_to_option({"alternative_index": 0}, trip_state, options_key="last_options", reason="test")
        assert "s1" in trip_state["rejected_station_ids"]

    def test_success_appends_decision(self):
        from src.bikeshare.agent import _switch_to_option
        trip_state = _make_trip_state(last_options=self._make_options())
        _switch_to_option({"alternative_index": 0}, trip_state, options_key="last_options", reason="test")
        decisions = trip_state["recent_decisions"]
        assert any(d.get("action") == "switch_target" for d in decisions)

    def test_no_options_returns_error(self):
        from src.bikeshare.agent import _switch_to_option
        trip_state = _make_trip_state()
        result = _switch_to_option({"alternative_index": 0}, trip_state, options_key="last_options", reason="test")
        assert result["action"] == "error"
        assert "option list" in result["message"]

    def test_out_of_bounds_returns_error(self):
        from src.bikeshare.agent import _switch_to_option
        trip_state = _make_trip_state(last_options=self._make_options())
        result = _switch_to_option({"alternative_index": 5}, trip_state, options_key="last_options", reason="test")
        assert result["action"] == "error"
        assert "not available" in result["message"]

    def test_negative_index_returns_error(self):
        from src.bikeshare.agent import _switch_to_option
        trip_state = _make_trip_state(last_options=self._make_options())
        result = _switch_to_option({"alternative_index": -1}, trip_state, options_key="last_options", reason="test")
        assert result["action"] == "error"


# ── handle_rider_command switch guard ─────────────────────────────────────────

class TestHandleRiderCommandSwitchGuard:
    def _make_options(self):
        return [{"station_id": "s2", "station_name": "Bay and Front", "docks_available": 6}]

    def test_accept_alternative_with_last_options_switches(self):
        trip_state = _make_trip_state(last_options=self._make_options())
        command = {"intent": "accept_alternative", "alternative_index": 0}
        result = handle_rider_command(command, trip_state)
        assert result["action"] == "switch_station"
        assert trip_state["target_station_id"] == "s2"

    def test_switch_intent_with_last_options_switches(self):
        trip_state = _make_trip_state(last_options=self._make_options())
        command = {"intent": "switch", "alternative_index": 0}
        result = handle_rider_command(command, trip_state)
        assert result["action"] == "switch_station"

    def test_switch_intent_without_last_options_falls_through(self):
        """Without last_options the switch intents should NOT call _switch_to_option."""
        trip_state = _make_trip_state()  # no last_options
        command = {"intent": "switch_station", "alternative_index": 0}
        result = handle_rider_command(command, trip_state)
        # Falls through to change_target path — action should not be switch_station
        assert result["action"] != "switch_station"
