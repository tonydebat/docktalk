"""Unit tests for app/monitor_task.py and related helpers in app/live_tools.py.

All GBFS I/O and tick calls are patched so tests run offline and fast.
Tests focus on observable behaviour:
- Alert is queued when run_background_monitor_tick produces an alert
- No alert is queued when tick produces no alert
- Signature-based dedup prevents re-speaking the same alert
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

from app.live_tools import build_alert_spoken_message, _derive_record_status
from app.monitor_task import run_monitor
from app.session_store import SessionRecord


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_session(status="monitoring") -> SessionRecord:
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


# ── build_alert_spoken_message ────────────────────────────────────────────────


def test_build_spoken_message_headline_and_message():
    alert = {"headline": "Docks are filling up.", "message": "Only 2 left.", "alternatives": []}
    msg = build_alert_spoken_message(alert)
    assert "Docks are filling up." in msg
    assert "Only 2 left." in msg


def test_build_spoken_message_with_alternatives():
    alert = {
        "headline": "Your target is at risk.",
        "message": "",
        "alternatives": [
            {"station_name": "Bay and Front", "docks_available": 7},
            {"station_name": "Wellington and York", "docks_available": 5},
        ],
    }
    msg = build_alert_spoken_message(alert)
    assert "Bay and Front" in msg
    assert "Wellington and York" in msg
    assert "7" in msg
    assert "5" in msg


def test_build_spoken_message_fallback():
    msg = build_alert_spoken_message({"headline": "", "message": "", "alternatives": []})
    assert len(msg) > 0


# ── run_monitor integration ───────────────────────────────────────────────────


def _run_async(coro):
    return asyncio.run(coro)


def test_monitor_queues_alert_on_warning():
    record = _make_session()
    sessions = {"test-1": record}

    def tick_with_alert_then_stop(r):
        r.trip_state["alert"] = {
            "type": "warning",
            "headline": "Docks are filling up.",
            "message": "Only 2 docks left.",
            "alternatives": [],
        }
        r.trip_state["status"] = "finished"
        return {"source": "monitor", "action": "alert"}

    with patch("app.monitor_task.run_background_monitor_tick", side_effect=tick_with_alert_then_stop):
        _run_async(
            run_monitor(
                "test-1",
                sessions,
                poll_interval_seconds=0,
            )
        )

    assert not record.alert_queue.empty()
    alert_msg = record.alert_queue.get_nowait()
    assert alert_msg.startswith("VERBATIM_ALERT:")


def test_monitor_no_alert_when_no_alert():
    record = _make_session()
    sessions = {"test-1": record}

    def tick_no_alert_then_stop(r):
        r.trip_state.pop("alert", None)
        r.trip_state["status"] = "finished"
        return {"source": "monitor", "action": "ok"}

    with patch("app.monitor_task.run_background_monitor_tick", side_effect=tick_no_alert_then_stop):
        _run_async(
            run_monitor(
                "test-1",
                sessions,
                poll_interval_seconds=0,
            )
        )

    assert record.alert_queue.empty()


def test_monitor_dedup_same_alert_signature():
    """Same headline+message should not be re-spoken on the second tick."""
    record = _make_session()
    sessions = {"test-1": record}

    tick_count = [0]

    def tick_same_alert_twice(r):
        tick_count[0] += 1
        r.trip_state["alert"] = {
            "headline": "Low docks.",
            "message": "Only 2 left.",
            "alternatives": [],
        }
        if tick_count[0] >= 2:
            r.trip_state["status"] = "finished"
        return {"source": "monitor", "action": "alert"}

    with patch("app.monitor_task.run_background_monitor_tick", side_effect=tick_same_alert_twice):
        _run_async(
            run_monitor(
                "test-1",
                sessions,
                poll_interval_seconds=0,
            )
        )

    # Alert should only be queued once (signature dedup)
    count = 0
    while not record.alert_queue.empty():
        record.alert_queue.get_nowait()
        count += 1
    assert count == 1


def test_monitor_terminates_when_stopped():
    record = _make_session(status="finished")
    sessions = {"test-1": record}

    with patch("app.monitor_task.run_background_monitor_tick") as mock_tick:
        _run_async(
            run_monitor(
                "test-1",
                sessions,
                poll_interval_seconds=0,
            )
        )
    # Monitor should exit without running any ticks since status is already finished
    mock_tick.assert_not_called()


def test_monitor_exits_when_session_removed():
    sessions: dict = {}
    # Should exit without error when session doesn't exist
    _run_async(
        run_monitor(
            "nonexistent-session",
            sessions,
            poll_interval_seconds=0,
        )
    )


# ── _derive_record_status ─────────────────────────────────────────────────────


def _make_ts(status="monitoring", alert=None, docks=None) -> dict:
    history = [{"docks_available": docks}] if docks is not None else []
    return {"status": status, "alert": alert, "dock_history": history}


@pytest.mark.parametrize("trip_state,expected", [
    (_make_ts(status="finished"),                  "STOPPED"),
    (_make_ts(status="alerted"),                   "ALERTED"),
    (_make_ts(docks=2),                            "MONITORING_WARNING"),
    (_make_ts(docks=1),                            "MONITORING_WARNING"),
    (_make_ts(docks=0),                            "MONITORING_WARNING"),
    (_make_ts(docks=3),                            "MONITORING_WATCH"),
    (_make_ts(docks=5),                            "MONITORING_WATCH"),
    (_make_ts(docks=6),                            "MONITORING_SAFE"),
    (_make_ts(),                                   "MONITORING_SAFE"),  # no history
])
def test_derive_record_status(trip_state, expected):
    assert _derive_record_status(trip_state) == expected

