"""Pure helpers for building and updating the trip_state dict.

trip_state is the single dict passed into — and mutated by — run_tick().
This module owns three responsibilities:

- make_initial_trip_state  : build a clean starting dict
- record_dock_observation  : append a live dock count to dock_history
- record_tick_decision     : read the agent's mutations and append to recent_decisions
"""

from datetime import datetime
from typing import Any


def make_initial_trip_state(
    station_id: str,
    station_name: str,
    arrival_time: datetime,
) -> dict[str, Any]:
    """Return a fresh trip_state dict ready to pass to run_tick().

    Args:
        station_id:   Bike Share Toronto station ID for the destination.
        station_name: Human-readable station name for the destination.
        arrival_time: When the rider expects to arrive (wall-clock datetime).
    """
    return {
        "target_station_id": station_id,
        "target_station_name": station_name,
        "arrival_time": arrival_time,
        "preferences": [],
        "dock_history": [],
        "recent_decisions": [],
        "rejected_station_ids": [],
        "target_just_switched": False,
        "status": "monitoring",
    }


def record_dock_observation(
    trip_state: dict[str, Any],
    docks_available: int,
    observed_at: str,
) -> None:
    """Append a live dock count entry to trip_state["dock_history"].

    Args:
        trip_state:     The live trip_state dict (mutated in place).
        docks_available: Number of available docks observed right now.
        observed_at:    ISO-8601 timestamp string for the observation.
    """
    trip_state["dock_history"].append({
        "observed_at": observed_at,
        "docks_available": docks_available,
    })


def record_tick_decision(trip_state: dict[str, Any]) -> None:
    """Read the agent's terminal action from trip_state and append to recent_decisions.

    run_tick() mutates trip_state in place via the action tools
    (alert_user, set_next_check, finish_trip). This function reads those
    mutations and records a summary entry that the agent will see on the
    next tick as recent context.

    Priority: finish_trip > alert_user > set_next_check.

    Args:
        trip_state: The trip_state dict after run_tick() has returned.
    """
    status = trip_state.get("status")

    if status == "finished":
        trip_state["recent_decisions"].append({
            "action": "finish_trip",
            "reason": trip_state.get("finish_reason", ""),
        })
    elif status == "alerted":
        alert = trip_state.get("alert", {})
        trip_state["recent_decisions"].append({
            "action": "alert_user",
            "headline": alert.get("headline", ""),
        })
    else:
        trip_state["recent_decisions"].append({
            "action": "set_next_check",
            "seconds": trip_state.get("next_check_seconds"),
            "reason": trip_state.get("next_check_reason", ""),
        })
