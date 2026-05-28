"""Conversation-layer tool registry for the Gemini Live session.

Maps Gemini Live tool names to Python handlers and provides the
FunctionDeclaration list for the LiveConnectConfig.

All tools follow the ownership rule: Python fetches data, scores stations,
and filters bad candidates before any results reach the model. The model
may only speak facts that come from these tool responses.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from google.genai import types

from src.bikeshare import destination_resolver as _resolver
from src.bikeshare import trip_state as _trip_state
from src.bikeshare.station_data import (
    fetch_all_stations,
    fetch_live_status,
    get_nearby_stations,
    get_station_status,
)
from src.bikeshare.station_search import search_stations

if TYPE_CHECKING:
    from app.session_store import SessionRecord

logger = logging.getLogger(__name__)

# ── Tool declarations ─────────────────────────────────────────────────────────

TOOL_DECLARATIONS: list[types.FunctionDeclaration] = [
    types.FunctionDeclaration(
        name="resolve_destination",
        description=(
            "Resolve a rider's spoken destination into ranked dock station candidates. "
            "Call this when the rider names a place to return their bike. "
            "Returns up to 5 recommendation objects with station name, dock count, and distance."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "transcript": types.Schema(
                    type=types.Type.STRING,
                    description="The rider's exact spoken words describing their destination.",
                ),
            },
            required=["transcript"],
        ),
    ),
    types.FunctionDeclaration(
        name="confirm_station",
        description=(
            "Confirm the rider's chosen station and start monitoring. "
            "Call this when the rider affirms the recommended station with 'yes', "
            "'confirm', 'go ahead', 'that one', or any similar affirmation after "
            "resolve_destination has returned candidates. Use the station_id and "
            "station_name of the top candidate you just presented to the rider. "
            "Returns the confirmed station name and starts the background monitor loop."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "station_id": types.Schema(
                    type=types.Type.STRING,
                    description="The station_id of the confirmed target station.",
                ),
                "station_name": types.Schema(
                    type=types.Type.STRING,
                    description="The human-readable name of the confirmed target station.",
                ),
            },
            required=["station_id", "station_name"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_station_status",
        description=(
            "Get the current live dock count for the target station. "
            "Use when the rider asks for an update and no monitor alert is pending."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "station_id": types.Schema(
                    type=types.Type.STRING,
                    description="The station_id to look up.",
                ),
            },
            required=["station_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_backup_options",
        description=(
            "Get up to 3 nearby backup stations the rider could switch to. "
            "Call when the rider asks 'what are my options?' "
            "Returns stations sorted by distance, excluding the current target, offline stations, "
            "and stations with 0 docks."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "station_id": types.Schema(
                    type=types.Type.STRING,
                    description="The current target station_id (used as the anchor for proximity).",
                ),
            },
            required=["station_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="switch_station",
        description=(
            "Switch the monitoring target to a new station. "
            "Call when the rider confirms they want to switch to a backup station."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "station_id": types.Schema(
                    type=types.Type.STRING,
                    description="The station_id of the new target station.",
                ),
                "station_name": types.Schema(
                    type=types.Type.STRING,
                    description="The human-readable name of the new target station.",
                ),
                "available_docks": types.Schema(
                    type=types.Type.INTEGER,
                    description=(
                        "Current number of available docks at the new station, "
                        "as returned by get_backup_options. Include whenever known "
                        "so the UI updates immediately without waiting for the next poll."
                    ),
                ),
            },
            required=["station_id", "station_name"],
        ),
    ),
    types.FunctionDeclaration(
        name="stop_monitoring",
        description=(
            "Stop monitoring and end the session. "
            "Call when the rider says they returned the bike, wants to cancel, "
            "or explicitly asks to stop monitoring."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "reason": types.Schema(
                    type=types.Type.STRING,
                    description="Brief reason for stopping (e.g. 'rider returned bike', 'cancelled').",
                ),
            },
            required=["reason"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_risk_summary",
        description=(
            "Run a monitor tick and return a spoken summary of the current risk level "
            "for the target station. Call when the rider asks 'any update?' or 'what is the status?'"
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "station_id": types.Schema(
                    type=types.Type.STRING,
                    description="The current target station_id.",
                ),
            },
            required=["station_id"],
        ),
    ),
]

GEMINI_TOOLS = [types.Tool(function_declarations=TOOL_DECLARATIONS)]

# ── Handler functions ─────────────────────────────────────────────────────────


def handle_resolve_destination(args: dict[str, Any], record: "SessionRecord") -> dict[str, Any]:
    transcript = args["transcript"]
    info = fetch_all_stations()
    status = fetch_live_status()
    stations = _resolver.merge_info_and_status(info, status)

    # Fast path: try the raw transcript as a direct name-search term first.
    # Gemini Live already performed STT, so the transcript is clean text —
    # often an exact or close match for a station name.  This avoids the
    # nested Gemini Flash HTTP call (parse_destination_intent) in the common
    # case, saving 2-5 s of round-trip latency.
    direct_hits = search_stations(transcript, stations)
    if direct_hits:
        candidates = [
            {
                "station_id": h.get("station_id", ""),
                "name": h.get("name", ""),
                "location_hint": h.get("name", ""),
                "available_docks": int(h.get("num_docks_available", 0) or 0),
                "distance_meters": int(h.get("distance_meters", 0) or 0),
                "station_status": h.get("station_status", "active"),
                "recommendation_reason": f"name match for '{transcript}'",
            }
            for h in direct_hits
        ]
        return {"candidates": candidates[:5]}

    # Slow path: full resolution cascade (Gemini Flash term extraction + geocoding).
    candidates = _resolver.resolve_destination(transcript, stations)
    if not candidates:
        return {"candidates": [], "message": "No stations found for that destination."}
    return {"candidates": candidates}


def handle_confirm_station(args: dict[str, Any], record: "SessionRecord") -> dict[str, Any]:
    station_id = args["station_id"]
    station_name = args["station_name"]
    from datetime import timedelta

    record.trip_state = _trip_state.make_initial_trip_state(
        station_id=station_id,
        station_name=station_name,
        arrival_time=datetime.now() + timedelta(minutes=15),
    )
    record.status = "MONITORING_SAFE"
    # Signal to the bridge that a monitor task should be spawned
    record.spawn_monitor = True
    return {"confirmed": True, "station_id": station_id, "station_name": station_name}


def handle_get_station_status(args: dict[str, Any], record: "SessionRecord") -> dict[str, Any]:
    station_id = args["station_id"]
    status = get_station_status(station_id)
    return {
        "station_id": station_id,
        "station_name": status.get("name", ""),
        "available_docks": status.get("num_docks_available", 0),
        "station_status": status.get("station_status", "unknown"),
        "observed_at": status.get("observed_at", ""),
    }


def handle_get_backup_options(args: dict[str, Any], record: "SessionRecord") -> dict[str, Any]:
    station_id = args["station_id"]
    nearby = get_nearby_stations(station_id, max_results=5, max_radius_m=800, min_docks=1)
    # Filter: exclude current target, offline, zero docks
    options = [
        {
            "station_id": s["station_id"],
            "name": s["name"],
            "available_docks": s["docks_available"],
            "distance_meters": s["distance_m"],
            "station_status": s.get("station_status", "active"),
        }
        for s in nearby
        if s["station_id"] != station_id
        and s.get("station_status", "active") == "active"
        and s["docks_available"] > 0
    ][:3]
    return {"options": options}


def handle_switch_station(args: dict[str, Any], record: "SessionRecord") -> dict[str, Any]:
    new_id = args["station_id"]
    new_name = args["station_name"]
    available_docks = args.get("available_docks")
    if record.trip_state:
        old_id = record.trip_state.get("target_station_id")
        if old_id:
            record.trip_state.setdefault("rejected_station_ids", []).append(old_id)
        record.trip_state["target_station_id"] = new_id
        record.trip_state["target_station_name"] = new_name
        record.trip_state["target_just_switched"] = True
        record.trip_state["status"] = "monitoring"
        record.trip_state["alert"] = None
        # Seed dock_history with the known count so the UI updates immediately
        # rather than showing "—" until the next monitor poll.
        record.trip_state["dock_history"] = (
            [{"observed_at": datetime.now().isoformat(), "docks_available": available_docks}]
            if available_docks is not None
            else []
        )
    record.status = "MONITORING_SAFE"
    return {"switched": True, "station_id": new_id, "station_name": new_name}


def handle_stop_monitoring(args: dict[str, Any], record: "SessionRecord") -> dict[str, Any]:
    reason = args.get("reason", "")
    record.status = "STOPPED"
    if record.trip_state:
        record.trip_state["status"] = "finished"
        record.trip_state["finish_reason"] = reason
    return {"stopped": True, "reason": reason}


def handle_get_risk_summary(args: dict[str, Any], record: "SessionRecord") -> dict[str, Any]:
    """Run a monitor tick synchronously and return a spoken summary."""
    from src.bikeshare.agent import run_tick
    from src.bikeshare.station_data import fetch_live_status as _fetch_live
    from src.bikeshare.trip_state import record_dock_observation

    if not record.trip_state:
        return {"spoken_message": "Monitoring has not started yet."}

    station_id = args.get("station_id") or record.trip_state.get("target_station_id", "")
    try:
        live = _fetch_live()
        docks = live.get(station_id, {}).get("num_docks_available", 0)
        record_dock_observation(
            record.trip_state,
            docks_available=docks,
            observed_at=datetime.now().isoformat(),
        )
        result = run_tick(record.trip_state)
        alert = record.trip_state.get("alert")
        if alert:
            spoken = f"{alert.get('headline', '')} {alert.get('message', '')}".strip()
        else:
            spoken = f"Your target station has {docks} open docks."
        return {"spoken_message": spoken}
    except Exception as exc:
        logger.warning("get_risk_summary failed: %s", exc)
        return {"spoken_message": "Live dock data is temporarily unavailable."}


# ── Dispatcher ────────────────────────────────────────────────────────────────

_HANDLERS = {
    "resolve_destination": handle_resolve_destination,
    "confirm_station": handle_confirm_station,
    "get_station_status": handle_get_station_status,
    "get_backup_options": handle_get_backup_options,
    "switch_station": handle_switch_station,
    "stop_monitoring": handle_stop_monitoring,
    "get_risk_summary": handle_get_risk_summary,
}


async def dispatch(
    tool_name: str,
    args: dict[str, Any],
    record: "SessionRecord",
) -> dict[str, Any]:
    """Dispatch a Gemini Live tool call to the appropriate Python handler.

    Runs the handler in a thread executor so blocking GBFS/Gemini calls
    don't stall the asyncio event loop.
    """
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        logger.warning("Unknown tool called: %s", tool_name)
        return {"error": f"unknown tool: {tool_name}"}
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, handler, args, record)
