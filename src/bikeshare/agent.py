# Beginner notes:
# This is the LLM-driven monitor loop, using Gemini's function calling protocol.
# One call to run_tick() = one agent wake-up. The agent decides what to do
# (continue, alert, finish) by calling the 6 tools defined in src/bikeshare/tools.py.
#
# If the LLM call fails or takes too long, fallback_tick() takes over with the
# deterministic if/else policy. Same shape of output. Demo never dies on API errors.
#
# Requirements:
#   pip install google-genai python-dotenv
#   GEMINI_API_KEY in your .env file (or in the environment)
#
# Run a single-tick test with: python src/bikeshare/agent.py

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, load_dotenv
from google import genai
from google.genai import types

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.bikeshare import tools as agent_tools


_dotenv_path = find_dotenv()
if _dotenv_path:
    load_dotenv(_dotenv_path)


MODEL = "gemini-2.5-flash"
MAX_TOOL_CALLS_PER_TICK = 5
LLM_TIMEOUT_MS = 60000
INTERVAL_TIMEOUT_MS = 8000
CHECK_IN_GRACE_MINUTES = 30
MAX_DOCK_HISTORY = 20

PAUSED_STATUSES = {"alerted", "check_in", "finished"}

_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompt" / "04_monitor_agent.txt"


def _load_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


ACTION_TOOLS = {"alert_user", "set_next_check", "finish_trip"}


def _build_gemini_tool() -> types.Tool:
    """Wrap our tool schemas from agent_tools.TOOL_SCHEMAS into a Gemini Tool object."""
    declarations = []
    for schema in agent_tools.TOOL_SCHEMAS:
        declarations.append(
            types.FunctionDeclaration(
                name=schema["name"],
                description=schema["description"],
                parameters=schema["input_schema"],
            )
        )
    return types.Tool(function_declarations=declarations)


def _build_user_message(trip_state: dict[str, Any]) -> str:
    now = datetime.now()
    arrival = trip_state.get("arrival_time")
    if isinstance(arrival, datetime):
        minutes_to_arrival = max(0, int((arrival - now).total_seconds() // 60))
        arrival_line = f"in {minutes_to_arrival} minutes (at {arrival.strftime('%H:%M')})"
    else:
        minutes_to_arrival = int(trip_state.get("minutes_to_arrival", 0))
        arrival_line = f"in about {minutes_to_arrival} minutes"

    recent_obs = trip_state.get("dock_history", [])[-5:]
    recent_decisions = trip_state.get("recent_decisions", [])[-3:]

    rejected = trip_state.get("rejected_station_ids", [])
    rejected_line = f"Rejected station ids (do not recommend): {rejected}" if rejected else "Rejected station ids: none"
    status = trip_state.get("status", "monitoring")

    return f"""Trip state snapshot:

Target station: {trip_state['target_station_name']} (id={trip_state['target_station_id']})
Monitoring status: {status}
Estimated arrival: {arrival_line}
Preferences: {trip_state.get('preferences', [])}
Target just switched this tick? {trip_state.get('target_just_switched', False)}
{rejected_line}

Recent dock observations (newest first):
{json.dumps(list(reversed(recent_obs)), indent=2) if recent_obs else 'none yet'}

Recent decisions (newest first):
{json.dumps(list(reversed(recent_decisions)), indent=2) if recent_decisions else 'none yet'}

Python handles cancellation, check-ins, and normal trip completion before calling you.
Do not call finish_trip in normal monitoring.
Decide the dock-risk monitoring action. Use tools as needed and end with exactly one of:
alert_user or set_next_check.
"""


def observe_target_station(trip_state: dict[str, Any]) -> dict[str, Any]:
    """Fetch the target station once and append that observation before Gemini reasons."""
    status = agent_tools.get_station_status(trip_state["target_station_id"])
    observation = {
        "observed_at": status["observed_at"],
        "docks_available": status["num_docks_available"],
        "station_status": status["station_status"],
        "is_returning": status["is_returning"],
    }

    dock_history = trip_state.setdefault("dock_history", [])
    dock_history.append(observation)
    if len(dock_history) > MAX_DOCK_HISTORY:
        del dock_history[:-MAX_DOCK_HISTORY]

    trip_state["latest_station_status"] = status
    return status


def apply_lifecycle_checks(
    trip_state: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Handle non-Gemini lifecycle states before the monitor agent runs."""
    now = now or datetime.now()
    status = trip_state.get("status", "monitoring")

    if status in PAUSED_STATUSES:
        return {
            "source": "lifecycle",
            "action": "skip",
            "reason": f"status is {status}",
            "trip_state": trip_state,
        }

    arrival = trip_state.get("arrival_time")
    if isinstance(arrival, datetime):
        check_in_at = arrival + timedelta(minutes=CHECK_IN_GRACE_MINUTES)
        if now >= check_in_at:
            trip_state["status"] = "check_in"
            trip_state["check_in"] = {
                "reason": f"arrival estimate passed {CHECK_IN_GRACE_MINUTES} minutes ago",
                "message": "Are you still riding, or should I stop monitoring?",
            }
            return {
                "source": "lifecycle",
                "action": "check_in",
                "reason": "arrival grace period passed",
                "trip_state": trip_state,
            }

    return None


def _append_recent_decision(trip_state: dict[str, Any], decision: dict[str, Any]) -> None:
    recent_decisions = trip_state.setdefault("recent_decisions", [])
    recent_decisions.append(decision)
    if len(recent_decisions) > 10:
        del recent_decisions[:-10]


def _format_station_update(status: dict[str, Any]) -> str:
    station_name = status.get("name") or status.get("station_id", "the target station")
    docks = status.get("num_docks_available", 0)
    station_status = status.get("station_status", "unknown")
    is_returning = int(status.get("is_returning", 0))

    if station_status != "active" or is_returning == 0:
        return f"{station_name} is not accepting returns right now."
    if docks == 1:
        return f"{station_name} has 1 open dock right now. I am still watching it."
    return f"{station_name} has {docks} open docks right now. I am still watching it."


def _format_options(options: list[dict[str, Any]]) -> str:
    if not options:
        return "I do not see a good nearby backup station right now. I will keep watching."

    phrases = []
    for option in options[:3]:
        station_name = option.get("station_name") or option.get("name", "nearby station")
        docks = option.get("docks_available", 0)
        walking_minutes = option.get("walking_minutes")
        if walking_minutes is None:
            phrases.append(f"{station_name} with {docks} docks")
        else:
            phrases.append(f"{station_name} with {docks} docks, about {walking_minutes} minutes away")

    if len(phrases) == 1:
        return f"One nearby option is {phrases[0]}."
    return "Nearby options are " + "; ".join(phrases) + "."


def _safe_get_nearby_stations(station_id: str, radius_m: int = 800) -> list[dict[str, Any]]:
    try:
        return agent_tools.get_nearby_stations(station_id, radius_m=radius_m)
    except Exception:
        return []


def _switch_to_option(
    command: dict[str, Any],
    trip_state: dict[str, Any],
    *,
    options_key: str,
    reason: str,
) -> dict[str, Any]:
    now = datetime.now()
    options = trip_state.get(options_key, [])
    alternative_index = int(command.get("alternative_index", 0))

    if not options:
        return {
            "source": "rider_command",
            "action": "error",
            "message": "I do not have a recent option list. Ask for options again.",
            "trip_state": trip_state,
        }
    if alternative_index < 0 or alternative_index >= len(options):
        return {
            "source": "rider_command",
            "action": "error",
            "message": "That option is not available. Ask for a different option.",
            "trip_state": trip_state,
        }

    chosen = options[alternative_index]
    old_station_id = trip_state["target_station_id"]
    rejected_station_ids = trip_state.setdefault("rejected_station_ids", [])
    if old_station_id not in rejected_station_ids:
        rejected_station_ids.append(old_station_id)

    trip_state["target_station_id"] = chosen["station_id"]
    trip_state["target_station_name"] = (
        chosen.get("station_name") or chosen.get("name", chosen["station_id"])
    )
    trip_state["dock_history"] = []
    trip_state["alert"] = None
    trip_state["status"] = "monitoring"
    trip_state["target_just_switched"] = True
    trip_state["next_check_seconds"] = 20
    trip_state["next_check_reason"] = "target switched - confirm new station still has docks"
    trip_state["next_check_at"] = now + timedelta(seconds=20)
    _append_recent_decision(
        trip_state,
        {
            "action": "switch_target",
            "from_station_id": old_station_id,
            "to_station_id": chosen["station_id"],
            "reason": reason,
        },
    )
    return {
        "source": "rider_command",
        "action": "switch_station",
        "message": f"Switching to {trip_state['target_station_name']}. I will keep monitoring it.",
        "chosen": chosen,
        "trip_state": trip_state,
    }


def handle_rider_command(
    command: dict[str, Any],
    trip_state: dict[str, Any],
) -> dict[str, Any]:
    """Handle rider-initiated commands without waiting for a Gemini tick."""
    intent = str(command.get("intent", "")).strip().lower()

    if intent in {"get_update", "update", "station_update"}:
        status = observe_target_station(trip_state)
        message = _format_station_update(status)
        _append_recent_decision(
            trip_state,
            {
                "action": "get_update",
                "station_id": trip_state["target_station_id"],
                "reason": "rider asked for station status",
            },
        )
        return {
            "source": "rider_command",
            "action": "get_update",
            "message": message,
            "station_status": status,
            "trip_state": trip_state,
        }

    if intent in {"show_options", "options", "nearby_options"}:
        radius_m = max(1, min(800, int(command.get("radius_m", 800))))
        options = _safe_get_nearby_stations(
            trip_state["target_station_id"],
            radius_m=radius_m,
        )[:3]
        trip_state["last_options"] = options
        _append_recent_decision(
            trip_state,
            {
                "action": "show_options",
                "station_id": trip_state["target_station_id"],
                "option_count": len(options),
                "reason": "rider asked for alternatives",
            },
        )
        return {
            "source": "rider_command",
            "action": "show_options",
            "message": _format_options(options),
            "options": options,
            "trip_state": trip_state,
        }

    if intent in {"switch_station", "switch", "accept_alternative"} and trip_state.get("last_options"):
        return _switch_to_option(
            command,
            trip_state,
            options_key="last_options",
            reason="rider accepted a nearby option",
        )

    if intent in {"cancel_monitoring", "cancel", "stop_monitoring"}:
        trip_state["status"] = "finished"
        trip_state["finish_reason"] = "rider cancelled monitoring"
        trip_state["alert"] = None
        _append_recent_decision(
            trip_state,
            {
                "action": "cancel_monitoring",
                "reason": "rider cancelled monitoring",
            },
        )
        return {
            "source": "rider_command",
            "action": "cancel_monitoring",
            "message": "Okay, I stopped monitoring.",
            "trip_state": trip_state,
        }

    if intent in {"finish_trip", "finished", "done"}:
        trip_state["status"] = "finished"
        trip_state["finish_reason"] = "rider confirmed trip finished"
        trip_state["alert"] = None
        _append_recent_decision(
            trip_state,
            {
                "action": "finish_trip",
                "reason": "rider confirmed trip finished",
            },
        )
        return {
            "source": "rider_command",
            "action": "finish_trip",
            "message": "Got it. Monitoring is complete.",
            "trip_state": trip_state,
        }

    if intent in {"change_target", "change_station", "switch_station"}:
        old_station_id = trip_state["target_station_id"]
        trip_state["status"] = "finished"
        trip_state["finish_reason"] = "rider requested a different target"
        trip_state["change_target_requested"] = True
        trip_state["alert"] = None
        _append_recent_decision(
            trip_state,
            {
                "action": "change_target_requested",
                "from_station_id": old_station_id,
                "reason": "rider requested a different target",
            },
        )
        return {
            "source": "rider_command",
            "action": "change_target",
            "message": "Sure. Where should I monitor instead?",
            "trip_state": trip_state,
        }

    return {
        "source": "rider_command",
        "action": "unknown",
        "message": "I did not catch that. You can ask for an update, ask for options, or stop monitoring.",
        "trip_state": trip_state,
    }


def apply_alert_response(
    response: dict[str, Any],
    trip_state: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply the rider's response to an active dock-risk alert."""
    now = now or datetime.now()
    intent = str(response.get("intent", "")).strip().lower()
    alert = trip_state.get("alert") or {}

    if intent in {"switch_station", "switch", "accept_alternative"}:
        alternatives = alert.get("alternatives", [])
        alternative_index = int(response.get("alternative_index", 0))
        if not alternatives:
            return {
                "source": "alert_response",
                "action": "error",
                "message": "I do not have an alternative station to switch to.",
                "trip_state": trip_state,
            }
        if alternative_index < 0 or alternative_index >= len(alternatives):
            return {
                "source": "alert_response",
                "action": "error",
                "message": "That option is not available. Ask for a different option.",
                "trip_state": trip_state,
            }

        chosen = alternatives[alternative_index]
        old_station_id = trip_state["target_station_id"]
        rejected_station_ids = trip_state.setdefault("rejected_station_ids", [])
        if old_station_id not in rejected_station_ids:
            rejected_station_ids.append(old_station_id)

        trip_state["target_station_id"] = chosen["station_id"]
        trip_state["target_station_name"] = chosen["station_name"]
        trip_state["dock_history"] = []
        trip_state["alert"] = None
        trip_state["status"] = "monitoring"
        trip_state["target_just_switched"] = True
        trip_state["next_check_seconds"] = 20
        trip_state["next_check_reason"] = "target switched - confirm new station still has docks"
        trip_state["next_check_at"] = now + timedelta(seconds=20)
        _append_recent_decision(
            trip_state,
            {
                "action": "switch_target",
                "from_station_id": old_station_id,
                "to_station_id": chosen["station_id"],
                "reason": "rider accepted alert alternative",
            },
        )
        return {
            "source": "alert_response",
            "action": "switch_station",
            "message": f"Switching to {chosen['station_name']}. I will keep monitoring it.",
            "chosen": chosen,
            "trip_state": trip_state,
        }

    if intent in {"keep_target", "keep", "stay"}:
        trip_state["alert"] = None
        trip_state["status"] = "monitoring"
        trip_state["next_check_seconds"] = 20
        trip_state["next_check_reason"] = "rider kept target after alert"
        trip_state["next_check_at"] = now + timedelta(seconds=20)
        _append_recent_decision(
            trip_state,
            {
                "action": "keep_target",
                "station_id": trip_state["target_station_id"],
                "reason": "rider rejected alert alternative",
            },
        )
        return {
            "source": "alert_response",
            "action": "keep_target",
            "message": "Okay, I will keep watching your original station.",
            "trip_state": trip_state,
        }

    if intent in {"cancel_monitoring", "cancel", "stop_monitoring"}:
        trip_state["alert"] = None
        trip_state["status"] = "finished"
        trip_state["finish_reason"] = "rider cancelled monitoring from alert"
        _append_recent_decision(
            trip_state,
            {
                "action": "cancel_monitoring",
                "reason": "rider cancelled monitoring from alert",
            },
        )
        return {
            "source": "alert_response",
            "action": "cancel_monitoring",
            "message": "Okay, I stopped monitoring.",
            "trip_state": trip_state,
        }

    return {
        "source": "alert_response",
        "action": "unknown",
        "message": "I did not catch that. You can switch, keep this station, or stop monitoring.",
        "trip_state": trip_state,
    }


def run_monitor_tick(trip_state: dict[str, Any]) -> dict[str, Any]:
    """One full monitor wake-up: Python observes, then Gemini reasons."""
    lifecycle_result = apply_lifecycle_checks(trip_state)
    if lifecycle_result is not None:
        return lifecycle_result

    observe_target_station(trip_state)
    return run_tick(trip_state)



def run_tick(trip_state: dict[str, Any]) -> dict[str, Any]:
    """One agent wake-up. Returns the trace (tool calls + reasoning) and final action."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return fallback_tick(trip_state)

    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=LLM_TIMEOUT_MS),
    )

    tool = _build_gemini_tool()

    config = types.GenerateContentConfig(
        temperature=0.0,
        system_instruction=_load_system_prompt(),
        tools=[tool],
    )

    contents: list[types.Content] = [
        types.Content(
            role="user",
            parts=[types.Part(text=_build_user_message(trip_state))],
        )
    ]
    trace: list[dict[str, Any]] = []

    try:
        tool_call_count = 0
        for _ in range(MAX_TOOL_CALLS_PER_TICK + 1):
            response = client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=config,
            )

            if not response.candidates:
                break

            candidate = response.candidates[0]
            model_parts = []
            if candidate.content is not None and candidate.content.parts is not None:
                model_parts = candidate.content.parts

            has_function_call = False
            function_responses: list[types.Part] = []
            action_tools_called: list[str] = []

            for part in model_parts:
                if getattr(part, "text", None):
                    trace.append({"type": "thinking", "text": part.text})

                function_call = getattr(part, "function_call", None)
                if function_call and function_call.name:
                    has_function_call = True
                    name = function_call.name
                    args = dict(function_call.args) if function_call.args else {}
                    result = agent_tools.dispatch(name, args, trip_state)
                    tool_call_count += 1
                    if name in ACTION_TOOLS:
                        action_tools_called.append(name)
                    trace.append({
                        "type": "tool_call",
                        "tool": name,
                        "args": args,
                        "result": result,
                    })
                    function_responses.append(
                        types.Part.from_function_response(
                            name=name,
                            response={"result": result},
                        )
                    )

            contents.append(types.Content(role="model", parts=model_parts))

            if not has_function_call:
                break

            if tool_call_count >= MAX_TOOL_CALLS_PER_TICK:
                trace.append({
                    "type": "tool_limit",
                    "message": f"Stopped after {tool_call_count} tool calls.",
                })
                break

            if action_tools_called:
                break

            contents.append(types.Content(role="user", parts=function_responses))

        return {"source": "llm", "trace": trace, "trip_state": trip_state}

    except Exception as exc:
        # Any Gemini error, network error, or timeout: fall back so the demo continues.
        trace.append({
            "type": "llm_error",
            "error_type": type(exc).__name__,
            "message": str(exc),
        })
        fallback = fallback_tick(trip_state)
        fallback["trace"] = trace + fallback["trace"]
        fallback["fallback_reason"] = f"llm_error: {type(exc).__name__}: {exc}"
        return fallback


def fallback_tick(trip_state: dict[str, Any]) -> dict[str, Any]:
    """Deterministic safety net. Same policy as monitoring_spec_updated.md fallback table."""
    status = trip_state.get("latest_station_status") or agent_tools.get_station_status(trip_state["target_station_id"])
    docks = status["num_docks_available"]
    station_status = status.get("station_status", "unknown")
    is_returning = int(status.get("is_returning", 0))

    now = datetime.now()
    arrival = trip_state.get("arrival_time")
    if isinstance(arrival, datetime):
        minutes_to_arrival = max(0, int((arrival - now).total_seconds() // 60))
    else:
        minutes_to_arrival = int(trip_state.get("minutes_to_arrival", 0))

    trace = [{
        "type": "fallback",
        "reason": "LLM unavailable or timed out",
        "station_status": station_status,
        "is_returning": is_returning,
        "observed_docks": docks,
        "minutes_to_arrival": minutes_to_arrival,
    }]

    station_name = trip_state.get("target_station_name", trip_state["target_station_id"])
    offline = station_status != "active" or is_returning == 0

    if offline:
        nearby = agent_tools.get_nearby_stations(trip_state["target_station_id"])
        agent_tools.dispatch(
            "alert_user",
            {
                "headline": "Destination station is offline.",
                "message": f"{station_name} is not accepting returns. Try one of these:",
                "alternatives": [
                    {
                        "station_id": s["station_id"],
                        "station_name": s["station_name"],
                        "docks_available": s["docks_available"],
                    }
                    for s in nearby[:3]
                ],
            },
            trip_state,
        )
        agent_tools.dispatch(
            "set_next_check",
            {"seconds": 20, "reason": "offline alert sent - continue monitoring urgently"},
            trip_state,
        )
        _append_recent_decision(trip_state, {"action": "alert_user", "reason": "station offline (fallback)"})
    elif docks == 0 and minutes_to_arrival >= 15:
        # Station is full but there is time to wait for a dock to open.
        agent_tools.dispatch(
            "set_next_check",
            {"seconds": 20, "reason": "station full but arrival is far - rechecking urgently"},
            trip_state,
        )
        _append_recent_decision(trip_state, {"action": "set_next_check", "seconds": 20, "reason": "station full, arrival far (fallback)"})
    elif docks < 2 and minutes_to_arrival < 15:
        nearby = agent_tools.get_nearby_stations(trip_state["target_station_id"])
        agent_tools.dispatch(
            "alert_user",
            {
                "headline": "Destination is filling up.",
                "message": f"{docks} docks left, arriving in {minutes_to_arrival} min. Try one of these:",
                "alternatives": [
                    {
                        "station_id": s["station_id"],
                        "station_name": s["station_name"],
                        "docks_available": s["docks_available"],
                    }
                    for s in nearby[:3]
                ],
            },
            trip_state,
        )
        agent_tools.dispatch(
            "set_next_check",
            {"seconds": 20, "reason": "low docks alert sent - continue monitoring urgently"},
            trip_state,
        )
        _append_recent_decision(trip_state, {"action": "alert_user", "reason": "low docks near arrival (fallback)"})
    elif docks < 4:
        agent_tools.dispatch("set_next_check", {"seconds": 30, "reason": "low docks"}, trip_state)
        _append_recent_decision(trip_state, {"action": "set_next_check", "seconds": 30, "reason": "low docks (fallback)"})
    else:
        agent_tools.dispatch("set_next_check", {"seconds": 60, "reason": "stable"}, trip_state)
        _append_recent_decision(trip_state, {"action": "set_next_check", "seconds": 60, "reason": "stable (fallback)"})

    return {"source": "fallback", "trace": trace, "trip_state": trip_state}


def demo_trip_state() -> dict[str, Any]:
    from src.bikeshare.station_data import fetch_all_stations, fetch_live_status
    info = fetch_all_stations()
    status = fetch_live_status()
    station_id = "7202"  # York St / Queen St W (City Hall)
    station_name = info.get(station_id, {}).get("name", station_id)
    live_docks = status.get(station_id, {}).get("num_docks_available", 4)
    return {
        "target_station_id": station_id,
        "target_station_name": station_name,
        "arrival_time": datetime.now() + timedelta(minutes=10),
        "preferences": [],
        "dock_history": [
            {"observed_at": "t-4min", "docks_available": live_docks + 3},
            {"observed_at": "t-3min", "docks_available": live_docks + 2},
            {"observed_at": "t-2min", "docks_available": live_docks + 1},
            {"observed_at": "t-1min", "docks_available": live_docks},
        ],
        "recent_decisions": [
            {"action": "set_next_check", "seconds": 60, "reason": "stable"},
        ],
        "status": "monitoring",
    }


def main() -> None:
    state = demo_trip_state()
    result = run_monitor_tick(state)
    print(f"Tick source: {result['source']}")
    if result.get("fallback_reason"):
        print(f"Fallback reason: {result['fallback_reason']}")
    print(f"Final state: {state.get('status')}, next_check_seconds={state.get('next_check_seconds')}")
    print("Trace:")
    for step in result["trace"]:
        print(json.dumps(step, indent=2, default=str))


if __name__ == "__main__":
    main()
