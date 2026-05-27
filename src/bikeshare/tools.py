# Beginner notes:
# This file defines the 6 tools the DockTalk monitor agent can call each tick.
# - TOOL_SCHEMAS is the JSON the LLM sees (what each tool is for and how to call it).
# - The functions below TOOL_SCHEMAS are the Python that actually runs.
# - dispatch() picks the right function when the LLM asks for one.
#
# Evidence tools: predict_fill_probability, get_nearby_stations, get_context.
# Action tools:   alert_user, set_next_check, finish_trip.
#
# The live-data path goes through src/bikeshare/station_data.py.
# Station profile metadata is read from data/station_context.csv and bundled
# into predict_fill_probability and get_nearby_stations responses, so there is
# no separate profile tool - the agent never needs to look it up alone.
# Run the self-tests with: python src/bikeshare/tools.py

import csv as _csv
import json
import sys
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.bikeshare import predictor
from src.bikeshare.station_data import (
    get_station_status as _get_station_status,
    get_nearby_stations as _get_nearby_stations,
)

_STATION_CONTEXT_PATH = Path(__file__).resolve().parents[2] / "data" / "station_context.csv"
_profile_cache: dict[str, dict[str, Any]] | None = None
_WALKING_SPEED_M_PER_MIN = 80  # ~4.8 km/h
_TORONTO_LAT = 43.6532
_TORONTO_LON = -79.3832
_WEATHER_CACHE_TTL = 600
_weather_cache: tuple[float, dict[str, Any]] | None = None

WEATHER_CODE_LABELS = {
    0: "clear",
    1: "mainly_clear",
    2: "partly_cloudy",
    3: "overcast",
    45: "fog",
    48: "rime_fog",
    51: "light_drizzle",
    53: "drizzle",
    55: "dense_drizzle",
    61: "light_rain",
    63: "rain",
    65: "heavy_rain",
    66: "freezing_rain",
    67: "heavy_freezing_rain",
    71: "light_snow",
    73: "snow",
    75: "heavy_snow",
    77: "snow_grains",
    80: "light_showers",
    81: "showers",
    82: "heavy_showers",
    85: "snow_showers",
    86: "heavy_snow_showers",
    95: "thunderstorm",
    96: "thunderstorm_hail",
    99: "severe_thunderstorm_hail",
}


def _is_true(value: Any) -> bool:
    return str(value).strip().lower() == "true"


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _weather_risk(
    weather_code: int,
    precipitation_mm: float,
    wind_kmh: float,
    temperature_c: float,
) -> str:
    if weather_code in {95, 96, 99}:
        return "storm"
    if weather_code in {66, 67, 71, 73, 75, 77, 85, 86}:
        return "snow_or_ice"
    if precipitation_mm >= 2.5 or weather_code in {63, 65, 81, 82}:
        return "rain"
    if precipitation_mm > 0 or weather_code in {51, 53, 55, 61, 80}:
        return "light_rain"
    if wind_kmh >= 40:
        return "windy"
    if temperature_c >= 30:
        return "hot"
    if temperature_c <= -5:
        return "cold"
    return "normal"


def _fetch_weather_context() -> dict[str, Any]:
    global _weather_cache
    now_ts = datetime.now().timestamp()
    if _weather_cache is not None and (now_ts - _weather_cache[0]) < _WEATHER_CACHE_TTL:
        return _weather_cache[1]

    params = urlencode({
        "latitude": _TORONTO_LAT,
        "longitude": _TORONTO_LON,
        "current": ",".join([
            "temperature_2m",
            "precipitation",
            "rain",
            "snowfall",
            "weather_code",
            "wind_speed_10m",
            "wind_gusts_10m",
        ]),
        "timezone": "America/Toronto",
    })
    url = f"https://api.open-meteo.com/v1/forecast?{params}"

    try:
        with urlopen(url, timeout=8) as response:
            payload = json.load(response)
        current = payload.get("current", {})
        weather_code = int(current.get("weather_code", -1))
        temperature_c = float(current.get("temperature_2m", 0.0))
        precipitation_mm = float(current.get("precipitation", 0.0))
        wind_kmh = float(current.get("wind_speed_10m", 0.0))
        weather = {
            "source": "open_meteo",
            "condition": WEATHER_CODE_LABELS.get(weather_code, "unknown"),
            "weather_code": weather_code,
            "temperature_c": round(temperature_c, 1),
            "precipitation_mm": round(precipitation_mm, 1),
            "rain_mm": round(float(current.get("rain", 0.0)), 1),
            "snowfall_cm": round(float(current.get("snowfall", 0.0)), 1),
            "wind_kmh": round(wind_kmh, 1),
            "wind_gust_kmh": round(float(current.get("wind_gusts_10m", 0.0)), 1),
            "weather_risk": _weather_risk(
                weather_code,
                precipitation_mm,
                wind_kmh,
                temperature_c,
            ),
            "observed_at": current.get("time", ""),
        }
    except Exception as exc:
        weather = {
            "source": "fallback",
            "condition": "unknown",
            "weather_code": None,
            "temperature_c": None,
            "precipitation_mm": None,
            "rain_mm": None,
            "snowfall_cm": None,
            "wind_kmh": None,
            "wind_gust_kmh": None,
            "weather_risk": "unknown",
            "observed_at": "",
            "error": f"{type(exc).__name__}: {exc}",
        }

    _weather_cache = (now_ts, weather)
    return weather


def _load_station_profiles() -> dict[str, dict[str, Any]]:
    global _profile_cache
    if _profile_cache is not None:
        return _profile_cache
    profiles: dict[str, dict[str, Any]] = {}
    with _STATION_CONTEXT_PATH.open(encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            profiles[row["station_id"]] = row
    _profile_cache = profiles
    return _profile_cache


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_nearby_stations",
        "description": (
            "List alternative stations within radius_m of the given station, with current docks, "
            "walking time, capacity class, area type, historical risk, and context hints. "
            "Use this when you need alternatives to recommend without making separate profile calls "
            "for every candidate."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "station_id": {"type": "string"},
                "radius_m": {"type": "integer"},
            },
            "required": ["station_id"],
        },
    },
    {
        "name": "get_context",
        "description": (
            "Get current time context: weekday/weekend, hour band "
            "(morning_rush_weekday, evening_rush_weekday, midday_weekday, etc), "
            "Toronto weather, and holiday flags. "
            "Use this to interpret whether a low dock count is normal or risky."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "predict_fill_probability",
        "description": (
            "Check live station health and estimate fill risk at arrival. "
            "Returns station_status (active/offline), is_returning, num_docks_available, "
            "and p_under_2_docks for the given arrival horizon. "
            "If the station is not active or not accepting returns, p_under_2_docks is 1.0 "
            "and the profile lookup is skipped. Use this as the first call every tick."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "station_id": {"type": "string"},
                "minutes_ahead": {"type": "integer"},
            },
            "required": ["station_id", "minutes_ahead"],
        },
    },
    {
        "name": "alert_user",
        "description": (
            "Send an alert to the rider. Use only when prediction or trend says risk is high. "
            "Provide a short headline, a one-sentence message, and up to 3 alternative stations "
            "ranked by safety (more docks first, then closer)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "headline": {"type": "string"},
                "message": {"type": "string"},
                "alternatives": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "station_id": {"type": "string"},
                            "station_name": {"type": "string"},
                            "docks_available": {"type": "integer"},
                            "reason": {"type": "string"},
                        },
                        "required": ["station_id", "station_name", "docks_available"],
                    },
                },
            },
            "required": ["headline", "message", "alternatives"],
        },
    },
    {
        "name": "set_next_check",
        "description": (
            "Continue monitoring. Choose when to wake up next, in seconds. "
            "Shorter intervals when risk is rising, longer when conditions are stable."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "integer"},
                "reason": {"type": "string"},
            },
            "required": ["seconds", "reason"],
        },
    },
    {
        "name": "finish_trip",
        "description": (
            "End monitoring. Call when the rider has arrived, cancelled, or the arrival "
            "window has clearly passed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
            },
            "required": ["reason"],
        },
    },
]


def get_station_status(station_id: str) -> dict[str, Any]:
    return _get_station_status(station_id)


def _station_profile_summary(station_id: str) -> dict[str, Any]:
    row = _load_station_profiles().get(station_id)
    if row is None:
        return {}
    return {
        "station_name": row.get("station_name", ""),
        "capacity": int(row["capacity"]) if row.get("capacity") else None,
        "capacity_class": row.get("capacity_class", ""),
        "primary_area_type": row.get("primary_area_type", ""),
        "context_tags": row.get("context_tags", ""),
        "context_summary": row.get("context_summary", ""),
        "is_charging_station": _is_true(row.get("is_charging_station")),
        "is_superstation": _is_true(row.get("is_superstation")),
        "valet_hours": row.get("valet_hours", ""),
        "historical_usage_class": row.get("historical_usage_class", "unknown"),
        "historical_return_risk_class": row.get("historical_return_risk_class", "unknown"),
        "historical_net_flow_per_day": _optional_float(row.get("historical_net_flow_per_day")),
        "agent_hint": row.get("agent_hint", ""),
    }


def get_nearby_stations(station_id: str, radius_m: int = 800) -> list[dict[str, Any]]:
    nearby = _get_nearby_stations(station_id, max_radius_m=radius_m, min_docks=1)
    enriched: list[dict[str, Any]] = []
    for station in nearby:
        distance_m = station["distance_m"]
        walking_minutes = max(1, round(distance_m / _WALKING_SPEED_M_PER_MIN))
        profile = _station_profile_summary(station["station_id"])
        enriched.append({
            **station,
            "station_name": profile.get("station_name") or station["name"],
            "walking_minutes": walking_minutes,
            **{k: v for k, v in profile.items() if k != "station_name"},
        })
    return enriched


def get_context() -> dict[str, Any]:
    now = datetime.now()
    return {
        "now_local_iso": now.isoformat(),
        "is_weekend": now.weekday() >= 5,
        "hour_band": predictor.classify_hour_band(now),
        "weather": _fetch_weather_context(),
        "is_holiday": False,
    }


def predict_fill_probability(station_id: str, minutes_ahead: int) -> dict[str, Any]:
    status = get_station_status(station_id)
    station_status = status.get("station_status", "unknown")
    is_returning = int(status.get("is_returning", 0))

    if station_status != "active" or is_returning == 0:
        return {
            "num_docks_available": status.get("num_docks_available", 0),
            "station_status": station_status,
            "is_returning": is_returning,
            "p_under_2_docks": 1.0,
            "predicted_docks": 0,
            "basis": "station is not active or not accepting returns - prediction skipped",
            "confidence": "high",
        }

    prediction = predictor.predict_fill_probability(
        station_id=station_id,
        minutes_ahead=minutes_ahead,
        current_docks=status["num_docks_available"],
        capacity=status["capacity"],
        now=datetime.now(),
    )
    return {
        "num_docks_available": status["num_docks_available"],
        "station_status": station_status,
        "is_returning": is_returning,
        **prediction,
    }


def dispatch(name: str, args: dict[str, Any], trip_state: dict[str, Any]) -> dict[str, Any]:
    if name == "get_nearby_stations":
        return {
            "nearby": get_nearby_stations(args["station_id"], args.get("radius_m", 800)),
        }
    if name == "get_context":
        return get_context()
    if name == "predict_fill_probability":
        return predict_fill_probability(args["station_id"], args["minutes_ahead"])
    if name == "alert_user":
        trip_state["alert"] = args
        trip_state["status"] = "alerted"
        return {"acknowledged": True}
    if name == "set_next_check":
        trip_state["next_check_seconds"] = args["seconds"]
        trip_state["next_check_reason"] = args["reason"]
        if trip_state.get("status") not in {"alerted", "finished"}:
            trip_state["status"] = "monitoring"
        return {"acknowledged": True}
    if name == "finish_trip":
        trip_state["status"] = "finished"
        trip_state["finish_reason"] = args["reason"]
        return {"acknowledged": True}
    return {"error": f"unknown tool: {name}"}


def main() -> None:
    from src.bikeshare.station_data import fetch_all_stations
    real_id = next(iter(fetch_all_stations()))

    state: dict[str, Any] = {"status": "monitoring"}

    pred = dispatch("predict_fill_probability", {"station_id": real_id, "minutes_ahead": 10}, state)
    assert "p_under_2_docks" in pred
    assert "station_status" in pred
    assert "is_returning" in pred
    assert "num_docks_available" in pred

    nearby = dispatch("get_nearby_stations", {"station_id": real_id, "radius_m": 500}, state)
    assert isinstance(nearby["nearby"], list) and len(nearby["nearby"]) > 0
    assert "walking_minutes" in nearby["nearby"][0]
    assert "historical_return_risk_class" in nearby["nearby"][0]
    assert "context_summary" in nearby["nearby"][0]

    ctx = dispatch("get_context", {}, state)
    assert "hour_band" in ctx
    assert isinstance(ctx["weather"], dict)
    assert "weather_risk" in ctx["weather"]

    dispatch("set_next_check", {"seconds": 30, "reason": "low docks, check sooner"}, state)
    assert state["next_check_seconds"] == 30

    dispatch("alert_user", {
        "headline": "Test alert",
        "message": "Just verifying the path.",
        "alternatives": [],
    }, state)
    assert state["status"] == "alerted"

    dispatch("set_next_check", {"seconds": 20, "reason": "alert sent - checking soon"}, state)
    assert state["status"] == "alerted", "set_next_check must not overwrite alerted status"
    assert state["next_check_seconds"] == 20

    dispatch("finish_trip", {"reason": "arrival window passed"}, state)
    assert state["status"] == "finished"

    unknown = dispatch("get_station_status", {"station_id": real_id}, state)
    assert "error" in unknown, "removed tools must not be reachable through dispatch"

    tool_names = [schema["name"] for schema in TOOL_SCHEMAS]
    assert len(tool_names) == 6, f"expected 6 tools, got {len(tool_names)}: {tool_names}"
    assert "get_station_status" not in tool_names
    assert "get_station_profile" not in tool_names
    assert "calculate_walking_time" not in tool_names

    print("All 6 tools dispatched cleanly.")
    print("Final state:", state)


if __name__ == "__main__":
    main()
