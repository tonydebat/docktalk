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
    arrival = trip_state["arrival_time"]
    minutes_to_arrival = max(0, int((arrival - now).total_seconds() // 60))

    recent_obs = trip_state.get("dock_history", [])[-5:]
    recent_decisions = trip_state.get("recent_decisions", [])[-3:]

    rejected = trip_state.get("rejected_station_ids", [])
    rejected_line = f"Rejected station ids (do not recommend): {rejected}" if rejected else "Rejected station ids: none"

    return f"""Trip state snapshot:

Target station: {trip_state['target_station_name']} (id={trip_state['target_station_id']})
Estimated arrival: in {minutes_to_arrival} minutes (at {arrival.strftime('%H:%M')})
Preferences: {trip_state.get('preferences', [])}
Target just switched this tick? {trip_state.get('target_just_switched', False)}
{rejected_line}

Recent dock observations (newest first):
{json.dumps(list(reversed(recent_obs)), indent=2) if recent_obs else 'none yet'}

Recent decisions (newest first):
{json.dumps(list(reversed(recent_decisions)), indent=2) if recent_decisions else 'none yet'}

Decide your next action. Use tools as needed and end with exactly one of:
alert_user, set_next_check, or finish_trip.
"""


def run_tick(trip_state: dict[str, Any]) -> dict[str, Any]:
    """One agent wake-up. Returns the trace (tool calls + reasoning) and final action."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Put it in .env or set it in the environment."
        )

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

            if "finish_trip" in action_tools_called or "set_next_check" in action_tools_called:
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
    status = agent_tools.get_station_status(trip_state["target_station_id"])
    docks = status["num_docks_available"]
    station_status = status.get("station_status", "unknown")
    is_returning = int(status.get("is_returning", 0))

    now = datetime.now()
    minutes_to_arrival = max(0, int((trip_state["arrival_time"] - now).total_seconds() // 60))

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
    elif docks == 0 and minutes_to_arrival >= 15:
        # Station is full but there is time to wait for a dock to open.
        agent_tools.dispatch(
            "set_next_check",
            {"seconds": 20, "reason": "station full but arrival is far - rechecking urgently"},
            trip_state,
        )
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
    elif docks < 4:
        agent_tools.dispatch("set_next_check", {"seconds": 30, "reason": "low docks"}, trip_state)
    else:
        agent_tools.dispatch("set_next_check", {"seconds": 60, "reason": "stable"}, trip_state)

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
    result = run_tick(state)
    print(f"Tick source: {result['source']}")
    if result.get("fallback_reason"):
        print(f"Fallback reason: {result['fallback_reason']}")
    print(f"Final state: {state.get('status')}, next_check_seconds={state.get('next_check_seconds')}")
    print("Trace:")
    for step in result["trace"]:
        print(json.dumps(step, indent=2, default=str))


if __name__ == "__main__":
    main()
