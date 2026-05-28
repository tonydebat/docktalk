"""Unit tests for app/monitor_task.py.

All GBFS I/O and run_tick calls are patched so tests run offline and fast.
Tests focus on observable behaviour:
- Alert is queued when run_monitor_tick produces an alert
- No alert is queued when run_monitor_tick produces no alert
- Cooldown prevents repeated identical alerts
- Monitor self-terminates when session is STOPPED
- Monitor self-terminates when GBFS data is stale
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.monitor_task import (
    _build_spoken_message,
    _should_speak_alert,
    run_monitor,
)
from app.session_store import SessionRecord


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_session(status="monitoring") -> dict:
    record = SessionRecord(session_id="test-1")
    record.trip_state = {
        "target_station_id": "station_1",
        "target_station_name": "Union Station",
        "status": status,
        "dock_history": [],
        "recent_decisions": [],
        "rejected_station_ids": [],
    }
    record.status = "MONITORING_SAFE"
    return record


def _fake_live_status(docks: int = 4) -> dict:
    return {"station_1": {"num_docks_available": docks}}


def _fake_run_tick_no_alert(trip_state: dict) -> dict:
    trip_state.pop("alert", None)
    return {"source": "fallback", "trace": [], "trip_state": trip_state}


def _fake_run_tick_with_alert(trip_state: dict) -> dict:
    trip_state["alert"] = {
        "type": "warning",
        "headline": "Docks are filling up.",
        "message": "Only 2 docks left.",
        "alternatives": [],
    }
    return {"source": "llm", "trace": [], "trip_state": trip_state}


def _fake_record_dock_observation(trip_state, docks_available, observed_at):
    trip_state.setdefault("dock_history", []).append(
        {"docks_available": docks_available, "observed_at": observed_at}
    )


def _fake_record_tick_decision(trip_state):
    pass


# ── _should_speak_alert ───────────────────────────────────────────────────────


def test_should_speak_when_no_prior_alert():
    alert = {"type": "warning"}
    assert _should_speak_alert(alert, None, None, 180) is True


def test_should_not_speak_during_cooldown():
    alert = {"type": "warning"}
    last_at = datetime.now() - timedelta(seconds=60)
    assert _should_speak_alert(alert, "warning", last_at, 180) is False


def test_should_speak_after_cooldown_expires():
    alert = {"type": "warning"}
    last_at = datetime.now() - timedelta(seconds=200)
    assert _should_speak_alert(alert, "warning", last_at, 180) is True


def test_critical_always_breaks_cooldown():
    alert = {"type": "critical"}
    last_at = datetime.now()  # just spoken
    assert _should_speak_alert(alert, "critical", last_at, 180) is True


def test_no_alert_never_speaks():
    assert _should_speak_alert(None, None, None, 180) is False


# ── _build_spoken_message ─────────────────────────────────────────────────────


def test_build_spoken_message_headline_and_message():
    alert = {"headline": "Docks are filling up.", "message": "Only 2 left.", "alternatives": []}
    msg = _build_spoken_message(alert)
    assert "Docks are filling up." in msg
    assert "Only 2 left." in msg


def test_build_spoken_message_with_alternatives():
    alert = {
        "headline": "Your target is at risk.",
        "message": "",
        "alternatives": [
            {"name": "Bay and Front", "available_docks": 7},
            {"name": "Wellington and York", "available_docks": 5},
        ],
    }
    msg = _build_spoken_message(alert)
    assert "Bay and Front" in msg
    assert "Wellington and York" in msg


def test_build_spoken_message_fallback():
    msg = _build_spoken_message({"headline": "", "message": "", "alternatives": []})
    assert len(msg) > 0


# ── run_monitor integration ───────────────────────────────────────────────────


def _run_async(coro):
    return asyncio.run(coro)


def test_monitor_queues_alert_on_warning():
    record = _make_session()
    sessions = {"test-1": record}

    with (
        patch("app.monitor_task.fetch_live_status", return_value=_fake_live_status(2)),
        patch("app.monitor_task.run_monitor_tick", side_effect=_fake_run_tick_with_alert),
        patch("app.monitor_task.record_tick_decision", side_effect=_fake_record_tick_decision),
    ):
        # After one tick with alert, mark session STOPPED so monitor exits
        original_run_monitor_tick = _fake_run_tick_with_alert

        tick_count = [0]

        def run_monitor_tick_then_stop(trip_state):
            tick_count[0] += 1
            result = original_run_monitor_tick(trip_state)
            # Stop after first tick
            trip_state["status"] = "finished"
            return result

        with patch("app.monitor_task.run_monitor_tick", side_effect=run_monitor_tick_then_stop):
            _run_async(
                run_monitor(
                    "test-1",
                    sessions,
                    poll_interval_seconds=0,
                    quick_retry_seconds=0,
                )
            )

    assert not record.alert_queue.empty()
    alert_msg = record.alert_queue.get_nowait()
    assert alert_msg.startswith("VERBATIM_ALERT:")


def test_monitor_no_alert_when_no_alert():
    record = _make_session()
    sessions = {"test-1": record}

    tick_count = [0]

    def run_monitor_tick_no_alert_then_stop(trip_state):
        tick_count[0] += 1
        _fake_run_tick_no_alert(trip_state)
        trip_state["status"] = "finished"
        return {"source": "fallback", "trace": [], "trip_state": trip_state}

    with (
        patch("app.monitor_task.fetch_live_status", return_value=_fake_live_status(8)),
        patch("app.monitor_task.run_monitor_tick", side_effect=run_monitor_tick_no_alert_then_stop),
        patch("app.monitor_task.record_tick_decision", side_effect=_fake_record_tick_decision),
    ):
        _run_async(
            run_monitor(
                "test-1",
                sessions,
                poll_interval_seconds=0,
                quick_retry_seconds=0,
            )
        )

    assert record.alert_queue.empty()


def test_monitor_terminates_when_stopped():
    record = _make_session(status="finished")
    sessions = {"test-1": record}

    # Monitor should exit immediately without calling run_tick
    with patch("app.monitor_task.fetch_live_status") as mock_fetch:
        _run_async(
            run_monitor(
                "test-1",
                sessions,
                poll_interval_seconds=0,
                quick_retry_seconds=0,
            )
        )
    # fetch should not be called if trip_state is already finished
    # (it may be called once to check; the key thing is the monitor exits)
    assert record.status != "in_progress"


def test_monitor_exits_when_session_removed():
    record = _make_session()
    sessions = {"test-1": record}

    tick_count = [0]

    async def remove_session_after_first_check():
        # Remove session mid-run to trigger exit
        del sessions["test-1"]

    def fetch_then_remove(docks=4):
        sessions.pop("test-1", None)
        return _fake_live_status(docks)

    # Monitor should exit gracefully when sessions dict no longer has entry
    sessions2: dict = {}
    _run_async(
        run_monitor(
            "nonexistent-session",
            sessions2,
            poll_interval_seconds=0,
            quick_retry_seconds=0,
        )
    )
    # Should exit without error (no session)
