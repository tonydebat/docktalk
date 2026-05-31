"""Tool handlers for the Gemini Live voice session."""

from __future__ import annotations

import asyncio
import csv
import difflib
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from google.genai import types

from src.bikeshare.agent import (
    apply_alert_response,
    handle_rider_command,
    observe_target_station,
    run_monitor_tick,
)
from src.bikeshare.geocoding import geocode_to_nearby_stations
from src.bikeshare.station_data import (
    fetch_all_stations,
    fetch_live_status,
    get_nearby_ebike_stations,
    get_nearby_stations,
    get_station_status,
    haversine_m,
)

if TYPE_CHECKING:
    from app.session_store import SessionRecord


DEFAULT_ARRIVAL_MINUTES = 15
STATION_CONTEXT_PATH = Path(__file__).resolve().parents[1] / "data" / "station_context.csv"
QUERY_STOP_WORDS = {
    "and",
    "the",
    "near",
    "return",
    "bike",
    "dock",
    "station",
    "to",
    "at",
    "change",
    "target",
    "destination",
    "going",
    "monitor",
    "instead",
    "please",
    "new",
    "want",
    "need",
}
_station_context_cache: list[dict[str, str]] | None = None
LANDMARK_ALIASES: list[dict[str, Any]] = [
    {
        "name": "City Hall",
        "aliases": [
            "city hall",
            "toronto city hall",
            "nathan phillips square",
            "city hall toronto",
        ],
        "lat": 43.6535,
        "lon": -79.3841,
    },
    {
        "name": "St Lawrence Market",
        "aliases": [
            "st lawrence market",
            "saint lawrence market",
            "st. lawrence market",
            "lawrence market",
        ],
        "lat": 43.6487,
        "lon": -79.3716,
    },
]


TOOL_DECLARATIONS: list[types.FunctionDeclaration] = [
    types.FunctionDeclaration(
        name="begin_change_target",
        description=(
            "Start a target-change flow when the rider says 'change target', "
            "'change destination', or similar but has not named the new place yet. "
            "After calling this, ask where DockTalk should monitor instead."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={},
        ),
    ),
    types.FunctionDeclaration(
        name="resolve_destination",
        description="Resolve a spoken destination into live Bike Share station candidates.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "transcript": types.Schema(
                    type=types.Type.STRING,
                    description="The rider's spoken destination or place.",
                ),
            },
            required=["transcript"],
        ),
    ),
    types.FunctionDeclaration(
        name="confirm_station",
        description="Confirm the selected station and start background monitoring.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "station_id": types.Schema(type=types.Type.STRING),
                "station_name": types.Schema(type=types.Type.STRING),
            },
            required=["station_id", "station_name"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_risk_summary",
        description="Get the latest target station status for an update request.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "station_id": types.Schema(type=types.Type.STRING),
            },
            required=["station_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_target_description",
        description=(
            "Describe the current monitored destination station and nearby context. "
            "Call when the rider asks 'where is my destination?', 'where am I heading?', "
            "'tell me my target', or similar."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={},
        ),
    ),
    types.FunctionDeclaration(
        name="get_distance_to_target",
        description=(
            "Estimate how far the rider is from the current target station using browser "
            "location if available. Call when the rider asks 'am I far?', 'how far along am I?', "
            "'how much farther?', or similar."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={},
        ),
    ),
    types.FunctionDeclaration(
        name="get_backup_options",
        description="Get nearby backup stations for the current target.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "station_id": types.Schema(type=types.Type.STRING),
            },
            required=["station_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_nearby_ebike_stations",
        description=(
            "Find up to 3 stations nearest to the rider's current browser location "
            "that have available e-bikes and open return docks. Call when the rider "
            "asks where they can get an e-bike, asks for e-bike stations nearby, "
            "or wants to change target specifically to find an e-bike. Do not use "
            "this when the rider asks for e-bikes near the target station."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={},
        ),
    ),
    types.FunctionDeclaration(
        name="get_ebike_stations_near_target",
        description=(
            "Find up to 3 stations nearest to the current monitored target station "
            "that have available e-bikes and open return docks. Call when the rider "
            "asks for e-bikes near their target, destination, or monitored station."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={},
        ),
    ),
    types.FunctionDeclaration(
        name="switch_to_option",
        description="Switch to a numbered option from the most recent alert or options list.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "option_number": types.Schema(
                    type=types.Type.INTEGER,
                    description="The spoken option number, starting at 1.",
                ),
            },
            required=["option_number"],
        ),
    ),
    types.FunctionDeclaration(
        name="choose_station_by_role",
        description=(
            "Choose a station from the latest matched destination candidates by role. "
            "Use role 'recommended' when the rider says safer, recommended, best, or reliable. "
            "Use role 'closest' when the rider says closest, nearest, or says they will risk it."
        ),
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "role": types.Schema(
                    type=types.Type.STRING,
                    description="Either 'recommended' or 'closest'.",
                ),
            },
            required=["role"],
        ),
    ),
    types.FunctionDeclaration(
        name="switch_station",
        description="Switch monitoring to a station by id and name.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "station_id": types.Schema(type=types.Type.STRING),
                "station_name": types.Schema(type=types.Type.STRING),
            },
            required=["station_id", "station_name"],
        ),
    ),
    types.FunctionDeclaration(
        name="stop_monitoring",
        description="Stop monitoring because the rider is done or cancelled.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "reason": types.Schema(type=types.Type.STRING),
            },
            required=["reason"],
        ),
    ),
]

GEMINI_TOOLS = [types.Tool(function_declarations=TOOL_DECLARATIONS)]


def _query_words(text: str) -> list[str]:
    return [
        word
        for word in re.findall(r"[a-z0-9]+", _normalize_spoken_place(text).lower())
        if len(word) > 2 and word not in QUERY_STOP_WORDS
    ]


def _normalize_spoken_place(text: str) -> str:
    normalized = text.lower()
    replacements = {
        r"\bdan\s+forth\b": "danforth",
        r"\bdan\s+fourth\b": "danforth",
        r"\bdan\s+for\b": "danforth",
        r"\bgreen\s+wood\b": "greenwood",
        r"\bwood\s+bine\b": "woodbine",
        r"\bcox\s+well\b": "coxwell",
        r"\bbroad\s+view\b": "broadview",
        r"\bmain\s+street\b": "main",
        r"\bqueen\s+street\b": "queen",
        r"\bking\s+street\b": "king",
    }
    for pattern, replacement in replacements.items():
        normalized = re.sub(pattern, replacement, normalized)
    return normalized


def _is_change_target_request_without_place(text: str) -> bool:
    words = _query_words(text)
    lowered = text.lower()
    has_change_phrase = any(
        phrase in lowered
        for phrase in [
            "change target",
            "change destination",
            "change station",
            "new target",
            "different station",
        ]
    )
    return has_change_phrase and not words


def _clean_destination(text: str) -> str:
    cleaned = _normalize_spoken_place(text)
    cleaned = re.sub(
        r"\b(change|switch|set|update)\s+(my\s+)?(target|destination|station)\s+(to|for)?\b",
        " ",
        cleaned,
    )
    cleaned = re.sub(r"\b(i am|i'm|im|we are|we're)\s+(going|heading)\s+(to|toward|towards)\b", " ", cleaned)
    cleaned = re.sub(r"\b(i need|i want|please)\s+(to\s+)?(return|dock|monitor)?\s*(near|at|to)?\b", " ", cleaned)
    cleaned = re.sub(r"\b(return|dock|bike|monitor|watch|instead|please|target|destination)\b", " ", cleaned)
    cleaned = re.sub(r"\b(in\s+)?(?:about|around)?\s*\d+\s*(min|minute|minutes)\b", " ", cleaned)
    return " ".join(cleaned.split()).strip()


def _station_matches_query(station_name: str, query: str) -> bool:
    words = _query_words(query)
    if not words:
        return False
    station_lower = _normalize_spoken_place(station_name)
    normalized_query = _normalize_spoken_place(query).strip()
    if normalized_query in station_lower:
        return True
    return _all_words_match_context(words, station_lower)


def _load_station_context() -> list[dict[str, str]]:
    global _station_context_cache
    if _station_context_cache is None:
        with STATION_CONTEXT_PATH.open(encoding="utf-8-sig", newline="") as f:
            _station_context_cache = list(csv.DictReader(f))
    return _station_context_cache


def _station_context_row(station_id: str) -> dict[str, str] | None:
    for row in _load_station_context():
        if row.get("station_id") == station_id:
            return row
    return None


def _context_search_text(row: dict[str, str]) -> str:
    fields = [
        "station_name",
        "nearest_subway_station",
        "nearest_park_or_recreation",
        "nearest_tourist_or_event_place",
        "nearest_major_road",
        "context_tags",
        "context_summary",
    ]
    return " ".join(row.get(field, "") for field in fields).lower()


def _tokenize_context(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _normalize_spoken_place(text).lower())


def _word_matches_context(word: str, tokens: list[str]) -> bool:
    if word in tokens:
        return True
    if any(word in token or token in word for token in tokens if len(token) > 3):
        return True
    return any(
        difflib.SequenceMatcher(None, word, token).ratio() >= 0.84
        for token in tokens
        if abs(len(word) - len(token)) <= 2
    )


def _all_words_match_context(words: list[str], text: str) -> bool:
    tokens = _tokenize_context(text)
    return all(_word_matches_context(word, tokens) for word in words)


def _normalize_landmark_text(text: str) -> str:
    normalized = _normalize_spoken_place(text)
    normalized = normalized.replace("st.", "st")
    normalized = re.sub(r"\bsaint\b", "st", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


def _landmark_for_query(query: str) -> dict[str, Any] | None:
    normalized_query = _normalize_landmark_text(query)
    for landmark in LANDMARK_ALIASES:
        for alias in landmark["aliases"]:
            if _normalize_landmark_text(alias) in normalized_query:
                return landmark
    return None


def _landmark_candidates(
    query: str,
    stations: dict[str, dict[str, Any]],
    statuses: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    landmark = _landmark_for_query(query)
    if not landmark:
        return []

    candidates: list[dict[str, Any]] = []
    for station_id, station in stations.items():
        lat = station.get("lat")
        lon = station.get("lon")
        if lat is None or lon is None:
            continue
        distance_m = haversine_m(
            float(landmark["lat"]),
            float(landmark["lon"]),
            float(lat),
            float(lon),
        )
        if distance_m > 900:
            continue
        candidate = _candidate_from_station(
            station_id,
            station,
            statuses.get(station_id, {}),
            f"near {landmark['name']}",
        )
        candidate["distance_meters"] = round(distance_m)
        candidate["landmark_name"] = landmark["name"]
        candidates.append(candidate)

    return _rank_with_recommended_and_closest(candidates)


def _rank_with_recommended_and_closest(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not candidates:
        return []

    for candidate in candidates:
        candidate.pop("candidate_role", None)

    candidates_by_recommendation = sorted(
        candidates,
        key=lambda candidate: (
            candidate["station_status"] != "active",
            candidate["is_returning"] == 0,
            candidate["available_docks"] <= 0,
            candidate["available_docks"] < 5,
            _candidate_distance(candidate) is None,
            _candidate_distance(candidate) is not None
            and _candidate_distance(candidate) > 500,
            -candidate.get("context_score", 0),
            -candidate["available_docks"],
            _candidate_distance(candidate) or 999_999,
        )
    )

    distance_candidates = [
        candidate for candidate in candidates if _candidate_distance(candidate) is not None
    ]
    candidates_by_distance = sorted(
        distance_candidates,
        key=lambda candidate: (
            candidate["station_status"] != "active",
            candidate["is_returning"] == 0,
            _candidate_distance(candidate),
            -candidate.get("context_score", 0),
        )
    )

    closest = candidates_by_distance[0] if candidates_by_distance else None
    if closest and _candidate_is_safe_enough(closest):
        recommended = closest
    else:
        recommended = candidates_by_recommendation[0]
    recommended["candidate_role"] = "recommended"

    result: list[dict[str, Any]] = [recommended]
    if closest and closest["station_id"] != recommended["station_id"]:
        closest["candidate_role"] = "closest"
        result.append(closest)

    for candidate in candidates_by_recommendation:
        if any(existing["station_id"] == candidate["station_id"] for existing in result):
            continue
        candidate["candidate_role"] = "nearby_option"
        result.append(candidate)
        if len(result) >= 5:
            break

    return result


def _candidate_distance(candidate: dict[str, Any]) -> int | None:
    distance = candidate.get("distance_meters")
    if isinstance(distance, (int, float)):
        return round(distance)
    return None


def _candidate_is_safe_enough(candidate: dict[str, Any]) -> bool:
    return (
        candidate["station_status"] == "active"
        and candidate["is_returning"] == 1
        and candidate["available_docks"] >= 5
    )


def _context_distance_for_query(row: dict[str, str], words: list[str]) -> int | None:
    distance_fields = [
        ("station_name", None),
        ("nearest_subway_station", "distance_to_subway_m"),
        ("nearest_park_or_recreation", "distance_to_park_m"),
        ("nearest_tourist_or_event_place", "distance_to_tourist_or_event_m"),
        ("nearest_major_road", "distance_to_major_road_m"),
    ]
    distances: list[int] = []
    for context_field, distance_field in distance_fields:
        field_text = row.get(context_field, "")
        matched_words = [
            word for word in words if _all_words_match_context([word], field_text)
        ]
        if not matched_words:
            continue
        if distance_field is None:
            distances.append(0)
            continue
        distance = _parse_distance_meters(row.get(distance_field, ""))
        if distance is not None:
            distances.append(distance)
    if not distances:
        return None
    return max(distances)


def _parse_distance_meters(value: str) -> int | None:
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return None


def _station_context_score(row: dict[str, str], words: list[str]) -> int:
    station_name = row.get("station_name", "").lower()
    strong_context = " ".join([
        row.get("nearest_subway_station", ""),
        row.get("nearest_park_or_recreation", ""),
        row.get("nearest_tourist_or_event_place", ""),
        row.get("nearest_major_road", ""),
    ]).lower()
    search_text = _context_search_text(row)

    if not _all_words_match_context(words, search_text):
        return 0

    score = 0
    for word in words:
        if _all_words_match_context([word], station_name):
            score += 4
        elif _all_words_match_context([word], strong_context):
            score += 3
        else:
            score += 1
    return score


def _candidate_from_station(
    station_id: str,
    station: dict[str, Any],
    status: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    return {
        "station_id": station_id,
        "station_name": station.get("name", ""),
        "available_docks": int(status.get("num_docks_available", 0) or 0),
        "station_status": status.get("station_status", "unknown"),
        "is_returning": int(status.get("is_returning", 0) or 0),
        "recommendation_reason": reason,
    }


def _context_candidates(
    query: str,
    stations: dict[str, dict[str, Any]],
    statuses: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    words = _query_words(query)
    if not words:
        return []

    candidates: list[dict[str, Any]] = []
    for row in _load_station_context():
        station_id = row.get("station_id", "")
        station = stations.get(station_id)
        if not station:
            continue
        score = _station_context_score(row, words)
        if score <= 0:
            continue
        candidate = _candidate_from_station(
            station_id,
            station,
            statuses.get(station_id, {}),
            "nearby context match",
        )
        candidate["context_score"] = score
        distance_m = _context_distance_for_query(row, words)
        if distance_m is not None:
            candidate["distance_meters"] = distance_m
        candidates.append(candidate)

    return _rank_with_recommended_and_closest(candidates)[:5]


def _best_context_candidates_with_relaxed_words(
    query: str,
    stations: dict[str, dict[str, Any]],
    statuses: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    words = _query_words(query)
    if len(words) < 2:
        return []

    candidates: list[dict[str, Any]] = []
    for row in _load_station_context():
        station_id = row.get("station_id", "")
        station = stations.get(station_id)
        if not station:
            continue
        search_text = _context_search_text(row)
        matched_count = sum(
            1 for word in words if _all_words_match_context([word], search_text)
        )
        if matched_count < max(2, len(words) - 1):
            continue
        score = matched_count * 2 + _station_context_score(row, [
            word for word in words if _all_words_match_context([word], search_text)
        ])
        candidate = _candidate_from_station(
            station_id,
            station,
            statuses.get(station_id, {}),
            "relaxed nearby context match",
        )
        candidate["context_score"] = score
        distance_m = _context_distance_for_query(row, words)
        if distance_m is not None:
            candidate["distance_meters"] = distance_m
        candidates.append(candidate)

    return _rank_with_recommended_and_closest(candidates)[:5]


def _geocode_candidates(
    query: str,
    stations: dict[str, Any],
    statuses: dict[str, Any],
) -> list[dict[str, Any]]:
    """Nominatim geocoding fallback — used when all name/context steps miss."""
    merged = {sid: {**info, **statuses.get(sid, {})} for sid, info in stations.items()}
    results = geocode_to_nearby_stations(query, merged, radius_m=1500, k=5)
    for r in results:
        r.setdefault("station_name", r.get("name", ""))
        r.setdefault("is_returning", 0)
    return results


def _resolve_destination_candidates(transcript: str) -> list[dict[str, Any]]:
    stations = fetch_all_stations()
    statuses = fetch_live_status()
    query = _clean_destination(transcript) or transcript.strip()

    landmark_matches = _landmark_candidates(query, stations, statuses)
    if landmark_matches:
        return landmark_matches

    candidates: list[dict[str, Any]] = []
    for station_id, station in stations.items():
        if not _station_matches_query(station.get("name", ""), query):
            continue
        candidates.append(
            _candidate_from_station(
                station_id,
                station,
                statuses.get(station_id, {}),
                "station name match",
            )
        )

    if candidates:
        return _rank_with_recommended_and_closest(candidates)[:5]
    context_matches = _context_candidates(query, stations, statuses)
    if context_matches:
        return context_matches
    relaxed = _best_context_candidates_with_relaxed_words(query, stations, statuses)
    if relaxed:
        return relaxed

    # Step 5: geocoding fallback — fires only when all name/context steps miss
    return _geocode_candidates(query, stations, statuses)


def _seed_dock_observation(record: "SessionRecord") -> None:
    if record.trip_state is None:
        return
    try:
        observe_target_station(record.trip_state)
    except Exception:
        pass


def _make_trip_state(station_id: str, station_name: str) -> dict[str, Any]:
    now = datetime.now()
    return {
        "target_station_id": station_id,
        "target_station_name": station_name,
        "eta_source": "live_voice_default",
        "minutes_to_arrival": DEFAULT_ARRIVAL_MINUTES,
        "arrival_time": now + timedelta(minutes=DEFAULT_ARRIVAL_MINUTES),
        "dock_history": [],
        "recent_decisions": [],
        "status": "monitoring",
        "alert": None,
        "next_check_seconds": 5,
        "next_check_reason": "initial live voice monitor check",
        "next_check_at": now,
    }


def handle_resolve_destination(
    args: dict[str, Any],
    record: "SessionRecord",
) -> dict[str, Any]:
    transcript = args["transcript"]
    if _is_change_target_request_without_place(transcript):
        return handle_begin_change_target({}, record)

    candidates = _resolve_destination_candidates(transcript)
    record.status = "CHOOSING_STATION" if candidates else "TARGET_NOT_FOUND"
    record.last_candidates = candidates[:3]
    record.last_options = []
    record.awaiting_new_target = False
    if candidates:
        top = candidates[0]
        closest = next(
            (
                candidate
                for candidate in candidates[1:]
                if candidate.get("candidate_role") == "closest"
            ),
            None,
        )
        if closest:
            closest_dock_phrase = (
                f"but only {_dock_count_phrase(closest['available_docks'])}"
                if closest["available_docks"] < 5
                else f"with {_dock_count_phrase(closest['available_docks'])}"
            )
            record.last_message = (
                f"Closest is {closest['station_name']}, "
                f"{closest.get('distance_meters')} meters away, "
                f"{closest_dock_phrase}. "
                f"Safer pick is {top['station_name']}, "
                f"{top.get('distance_meters')} meters away, with "
                f"{_dock_count_phrase(top['available_docks'])}. "
                "Which one do you want?"
            )
        else:
            distance = top.get("distance_meters")
            distance_phrase = f", {distance} meters away," if distance is not None else ""
            record.last_message = (
                f"Best choice is {top['station_name']}{distance_phrase} with "
                f"{_dock_count_phrase(top['available_docks'])}."
            )
    else:
        record.last_message = "No matching Bike Share station found. Try a nearby intersection or landmark."
    return {
        "candidates": candidates,
        "cleaned_destination": _clean_destination(transcript),
    }


def handle_begin_change_target(
    args: dict[str, Any],
    record: "SessionRecord",
) -> dict[str, Any]:
    record.status = "CHANGING_TARGET"
    record.awaiting_new_target = True
    record.last_candidates = []
    record.last_options = []
    if record.trip_state is not None:
        record.trip_state["alert"] = None
    record.last_message = "Where should DockTalk monitor instead?"
    return {
        "needs_destination": True,
        "message": record.last_message,
    }


def handle_confirm_station(
    args: dict[str, Any],
    record: "SessionRecord",
) -> dict[str, Any]:
    station_id = args["station_id"]
    station_name = args["station_name"]
    record.trip_state = _make_trip_state(station_id, station_name)
    _seed_dock_observation(record)
    record.status = _derive_record_status(record.trip_state)
    record.spawn_monitor = True
    record.last_candidates = []
    record.last_options = []
    record.last_message = f"Monitoring {station_name}."
    record.awaiting_new_target = False
    return {
        "confirmed": True,
        "station_id": station_id,
        "station_name": station_name,
        "message": f"Monitoring {station_name}.",
    }


def handle_get_risk_summary(
    args: dict[str, Any],
    record: "SessionRecord",
) -> dict[str, Any]:
    if record.trip_state is None:
        return {"spoken_message": "No station is being monitored yet."}

    result = handle_rider_command({"intent": "get_update"}, record.trip_state)
    record.last_message = result["message"]
    return {
        "spoken_message": result["message"],
        "station_status": result.get("station_status", {}),
    }


def handle_get_target_description(
    args: dict[str, Any],
    record: "SessionRecord",
) -> dict[str, Any]:
    if record.trip_state is None:
        return {"spoken_message": "No station is being monitored yet."}

    station_id = record.trip_state["target_station_id"]
    station_name = record.trip_state["target_station_name"]
    row = _station_context_row(station_id)

    details: list[str] = []
    if row:
        major_road = row.get("nearest_major_road", "").strip()
        subway = row.get("nearest_subway_station", "").strip()
        park = row.get("nearest_park_or_recreation", "").strip()
        tourist = row.get("nearest_tourist_or_event_place", "").strip()
        if major_road:
            details.append(f"near {major_road}")
        if subway:
            details.append(f"near {subway}")
        if tourist:
            details.append(f"near {tourist}")
        elif park:
            details.append(f"near {park}")

    if details:
        spoken = f"Your target is {station_name}, {', '.join(details[:2])}."
    else:
        spoken = f"Your target is {station_name}."

    record.last_message = spoken
    return {
        "station_id": station_id,
        "station_name": station_name,
        "spoken_message": spoken,
        "context": row or {},
    }


def handle_get_distance_to_target(
    args: dict[str, Any],
    record: "SessionRecord",
) -> dict[str, Any]:
    if record.trip_state is None:
        return {"spoken_message": "No station is being monitored yet."}

    station_id = record.trip_state["target_station_id"]
    station_name = record.trip_state["target_station_name"]
    stations = fetch_all_stations()
    station = stations.get(station_id, {})
    location = record.rider_location or {}

    if not station.get("lat") or not station.get("lon") or not location:
        spoken = _eta_fallback_message(record.trip_state, station_name)
        record.last_message = spoken
        return {
            "spoken_message": spoken,
            "location_available": False,
        }

    distance_m = haversine_m(
        float(location["lat"]),
        float(location["lon"]),
        float(station["lat"]),
        float(station["lon"]),
    )
    ride_minutes = max(1, round(distance_m / 200))
    distance_phrase = _format_distance(distance_m)
    accuracy = location.get("accuracy_m")
    accuracy_note = ""
    if isinstance(accuracy, (int, float)) and accuracy > 100:
        accuracy_note = " Location accuracy is rough."
    minute_phrase = "1 minute" if ride_minutes == 1 else f"{ride_minutes} minutes"

    spoken = (
        f"You are about {distance_phrase} from {station_name}, "
        f"roughly {minute_phrase} by bike.{accuracy_note}"
    )
    record.last_message = spoken
    return {
        "spoken_message": spoken,
        "location_available": True,
        "distance_m": round(distance_m),
        "rough_bike_minutes": ride_minutes,
        "accuracy_m": accuracy,
    }


def _eta_fallback_message(trip_state: dict[str, Any], station_name: str) -> str:
    arrival = trip_state.get("arrival_time")
    if isinstance(arrival, datetime):
        minutes = max(0, round((arrival - datetime.now()).total_seconds() / 60))
        return (
            f"Browser location is not available. Based on the trip estimate, "
            f"{station_name} is about {minutes} minutes away."
        )
    minutes = int(trip_state.get("minutes_to_arrival", DEFAULT_ARRIVAL_MINUTES))
    return (
        f"Browser location is not available. Based on the trip estimate, "
        f"{station_name} is about {minutes} minutes away."
    )


def _format_distance(distance_m: float) -> str:
    if distance_m < 1000:
        return f"{round(distance_m)} meters"
    return f"{distance_m / 1000:.1f} kilometers"


def _dock_count_phrase(count: int) -> str:
    noun = "dock" if count == 1 else "docks"
    return f"{count} open {noun}"


def _format_ebike_options(
    options: list[dict[str, Any]],
    *,
    intro: str = "Nearby e-bike options are",
    empty_message: str = "I do not see any nearby stations with both e-bikes and open docks right now.",
) -> str:
    if not options:
        return empty_message

    phrases = []
    for option in options[:3]:
        station_name = option.get("station_name") or option.get("name", "nearby station")
        ebikes = int(option.get("ebikes_available", 0) or 0)
        docks = int(option.get("docks_available", 0) or 0)
        distance = option.get("distance_m") or option.get("distance_meters")
        ebike_noun = "e-bike" if ebikes == 1 else "e-bikes"
        dock_noun = "dock" if docks == 1 else "docks"
        distance_phrase = f", {distance} meters away" if distance is not None else ""
        phrases.append(
            f"{station_name} with {ebikes} {ebike_noun} and {docks} open {dock_noun}{distance_phrase}"
        )

    return intro + " " + "; ".join(phrases) + "."


def handle_get_backup_options(
    args: dict[str, Any],
    record: "SessionRecord",
) -> dict[str, Any]:
    if record.trip_state is None:
        return {"options": [], "message": "No station is being monitored yet."}

    result = handle_rider_command({"intent": "show_options"}, record.trip_state)
    options = result.get("options", [])
    record.last_options = options[:3]
    record.last_candidates = []
    record.last_message = result["message"]
    return {"options": options, "spoken_message": result["message"]}


def handle_get_nearby_ebike_stations(
    args: dict[str, Any],
    record: "SessionRecord",
) -> dict[str, Any]:
    location = record.rider_location or {}
    lat = location.get("lat")
    lon = location.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        record.last_message = "I need your current location to find nearby e-bikes."
        return {
            "options": [],
            "spoken_message": record.last_message,
            "location_available": False,
            "battery_details_available": False,
        }

    target_station_id = None
    if record.trip_state is not None:
        target_station_id = record.trip_state.get("target_station_id")

    options = get_nearby_ebike_stations(
        float(lat),
        float(lon),
        max_results=3,
        exclude_station_id=target_station_id,
    )
    record.last_options = options[:3]
    record.last_candidates = []
    record.last_message = _format_ebike_options(options)
    return {
        "options": options,
        "spoken_message": record.last_message,
        "location_available": True,
        "battery_details_available": False,
        "battery_details_note": "Station availability shows e-bike counts, but not per-bike battery levels.",
    }


def handle_get_ebike_stations_near_target(
    args: dict[str, Any],
    record: "SessionRecord",
) -> dict[str, Any]:
    if record.trip_state is None:
        record.last_message = "No target station is being monitored yet."
        return {
            "options": [],
            "spoken_message": record.last_message,
            "target_available": False,
            "battery_details_available": False,
        }

    target_station_id = record.trip_state.get("target_station_id")
    target_station_name = record.trip_state.get("target_station_name", "your target")
    target_station = fetch_all_stations().get(target_station_id, {})
    lat = target_station.get("lat")
    lon = target_station.get("lon")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        record.last_message = "I do not have map coordinates for your target station."
        return {
            "options": [],
            "spoken_message": record.last_message,
            "target_available": False,
            "battery_details_available": False,
        }

    options = get_nearby_ebike_stations(
        float(lat),
        float(lon),
        max_results=3,
        exclude_station_id=target_station_id,
    )
    record.last_options = options[:3]
    record.last_candidates = []
    record.last_message = _format_ebike_options(
        options,
        intro=f"E-bike options near {target_station_name} are",
        empty_message=f"I do not see any e-bike stations near {target_station_name} right now.",
    )
    return {
        "options": options,
        "spoken_message": record.last_message,
        "target_available": True,
        "target_station_id": target_station_id,
        "target_station_name": target_station_name,
        "battery_details_available": False,
        "battery_details_note": "Station availability shows e-bike counts, but not per-bike battery levels.",
    }


def handle_switch_to_option(
    args: dict[str, Any],
    record: "SessionRecord",
) -> dict[str, Any]:
    option_number = max(1, int(args.get("option_number", 1)))
    candidate_result = _switch_to_matched_candidate(option_number, record)
    if candidate_result is not None:
        return candidate_result

    command = {
        "intent": "switch_station",
        "alternative_index": option_number - 1,
    }
    if record.trip_state is not None and record.trip_state.get("status") == "alerted":
        result = apply_alert_response(command, record.trip_state)
    elif record.last_options:
        result = _switch_to_visible_option(option_number, record)
    elif record.trip_state is None:
        return {"switched": False, "message": "No station option is available to monitor."}
    else:
        result = handle_rider_command(command, record.trip_state)
    if result.get("action") == "switch_station":
        _seed_dock_observation(record)
    record.status = _derive_record_status(record.trip_state)
    if result.get("action") == "switch_station":
        record.last_options = []
    record.last_message = result["message"]
    return {
        "switched": result.get("action") == "switch_station",
        "message": result["message"],
        "target_station_id": record.trip_state.get("target_station_id"),
        "target_station_name": record.trip_state.get("target_station_name"),
    }


def _switch_to_matched_candidate(
    option_number: int,
    record: "SessionRecord",
) -> dict[str, Any] | None:
    if not record.last_candidates:
        return None

    index = option_number - 1
    if index < 0 or index >= len(record.last_candidates):
        record.last_message = "That matched station is not available."
        return {
            "switched": False,
            "message": record.last_message,
        }

    chosen = record.last_candidates[index]
    station_id = chosen["station_id"]
    station_name = chosen["station_name"]
    if record.trip_state is None:
        result = handle_confirm_station(
            {"station_id": station_id, "station_name": station_name},
            record,
        )
    else:
        result = handle_switch_station(
            {"station_id": station_id, "station_name": station_name},
            record,
        )

    record.last_candidates = []
    record.last_message = f"Monitoring {station_name}."
    return {
        "switched": True,
        "message": record.last_message,
        "target_station_id": station_id,
        "target_station_name": station_name,
        "tool_result": result,
    }


def handle_choose_station_by_role(
    args: dict[str, Any],
    record: "SessionRecord",
) -> dict[str, Any]:
    role = str(args.get("role", "")).lower().strip()
    if role in {"safer", "safe", "best", "reliable"}:
        role = "recommended"
    if role in {"nearest", "close"}:
        role = "closest"

    if role not in {"recommended", "closest"}:
        record.last_message = "Please choose the safer station or the closest one."
        return {
            "switched": False,
            "message": record.last_message,
        }

    for index, candidate in enumerate(record.last_candidates, start=1):
        if candidate.get("candidate_role") == role:
            return handle_switch_to_option({"option_number": index}, record)

    record.last_message = f"The {role} station is not available anymore."
    return {
        "switched": False,
        "message": record.last_message,
    }


def _switch_to_visible_option(
    option_number: int,
    record: "SessionRecord",
) -> dict[str, Any]:
    index = option_number - 1
    if index < 0 or index >= len(record.last_options):
        return {
            "source": "rider_command",
            "action": "error",
            "message": "That option is not available. Ask for options again.",
            "trip_state": record.trip_state,
        }

    chosen = record.last_options[index]
    station_id = chosen["station_id"]
    station_name = chosen.get("station_name") or chosen.get("name", station_id)
    if record.trip_state is None:
        result = handle_confirm_station(
            {"station_id": station_id, "station_name": station_name},
            record,
        )
        message = f"Monitoring {station_name}."
    else:
        result = handle_switch_station(
            {"station_id": station_id, "station_name": station_name},
            record,
        )
        message = f"Switching to {station_name}. I will keep monitoring it."
    return {
        "source": "rider_command",
        "action": "switch_station",
        "message": message,
        "chosen": chosen,
        "trip_state": record.trip_state,
        "tool_result": result,
    }


def handle_switch_station(
    args: dict[str, Any],
    record: "SessionRecord",
) -> dict[str, Any]:
    if record.trip_state is None:
        record.trip_state = _make_trip_state(args["station_id"], args["station_name"])
    else:
        old_station_id = record.trip_state.get("target_station_id")
        if old_station_id:
            record.trip_state.setdefault("rejected_station_ids", []).append(old_station_id)
        record.trip_state["target_station_id"] = args["station_id"]
        record.trip_state["target_station_name"] = args["station_name"]
        record.trip_state["dock_history"] = []
        record.trip_state["alert"] = None
        record.trip_state["status"] = "monitoring"
        record.trip_state["target_just_switched"] = True
        record.trip_state["next_check_seconds"] = 20
        record.trip_state["next_check_reason"] = "target switched from live voice"
        record.trip_state["next_check_at"] = datetime.now() + timedelta(seconds=20)

    _seed_dock_observation(record)
    record.status = _derive_record_status(record.trip_state)
    record.last_candidates = []
    record.last_options = []
    record.last_message = f"Switching to {args['station_name']}. Monitoring continues."
    record.awaiting_new_target = False
    return {
        "switched": True,
        "station_id": args["station_id"],
        "station_name": args["station_name"],
    }


def handle_stop_monitoring(
    args: dict[str, Any],
    record: "SessionRecord",
) -> dict[str, Any]:
    record.status = "STOPPED"
    record.awaiting_new_target = False
    if record.trip_state is not None:
        result = handle_rider_command(
            {"intent": "finish_trip"},
            record.trip_state,
        )
        record.trip_state["finish_reason"] = args.get("reason") or result["message"]
    record.last_message = args.get("reason", "Monitoring stopped.")
    return {"stopped": True, "reason": args.get("reason", "")}


def _derive_record_status(trip_state: dict[str, Any]) -> str:
    status = trip_state.get("status", "monitoring")
    if status == "finished":
        return "STOPPED"
    if status == "alerted":
        return "ALERTED"
    latest = (trip_state.get("dock_history") or [{}])[-1]
    docks = latest.get("docks_available")
    if docks is None:
        return "MONITORING_SAFE"
    if docks <= 2:
        return "MONITORING_WARNING"
    if docks <= 5:
        return "MONITORING_WATCH"
    return "MONITORING_SAFE"


def _latest_target_ebikes(trip_state: dict[str, Any]) -> int | None:
    latest = (trip_state.get("dock_history") or [{}])[-1]
    ebikes = latest.get("ebikes_available")
    if ebikes is not None:
        return int(ebikes)
    latest_status = trip_state.get("latest_station_status") or {}
    if latest_status.get("ebikes_available") is not None:
        return int(latest_status["ebikes_available"])
    return None


def run_background_monitor_tick(record: "SessionRecord") -> dict[str, Any]:
    if record.trip_state is None:
        return {"source": "monitor", "action": "no_trip_state"}
    result = run_monitor_tick(record.trip_state)
    record.status = _derive_record_status(record.trip_state)
    alert = record.trip_state.get("alert")
    if alert:
        record.last_options = alert.get("alternatives", [])[:3]
        record.last_message = alert.get("message", "")
    return result


_HANDLERS = {
    "begin_change_target": handle_begin_change_target,
    "resolve_destination": handle_resolve_destination,
    "confirm_station": handle_confirm_station,
    "get_risk_summary": handle_get_risk_summary,
    "get_target_description": handle_get_target_description,
    "get_distance_to_target": handle_get_distance_to_target,
    "get_backup_options": handle_get_backup_options,
    "get_nearby_ebike_stations": handle_get_nearby_ebike_stations,
    "get_ebike_stations_near_target": handle_get_ebike_stations_near_target,
    "switch_to_option": handle_switch_to_option,
    "choose_station_by_role": handle_choose_station_by_role,
    "switch_station": handle_switch_station,
    "stop_monitoring": handle_stop_monitoring,
}


async def dispatch(
    tool_name: str,
    args: dict[str, Any],
    record: "SessionRecord",
) -> dict[str, Any]:
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        return {"error": f"unknown tool: {tool_name}"}
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, handler, args, record)


def _attach_coordinates(
    rows: list[dict[str, Any]],
    stations: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for row in rows:
        station_id = row.get("station_id")
        station = stations.get(station_id, {}) if station_id else {}
        lat = station.get("lat")
        lon = station.get("lon")
        merged = dict(row)
        if lat is not None and lon is not None:
            merged["lat"] = lat
            merged["lon"] = lon
        enriched.append(merged)
    return enriched


def format_status_payload(record: "SessionRecord") -> dict[str, Any]:
    trip_state = record.trip_state or {}
    alert = trip_state.get("alert") or {}
    options = alert.get("alternatives") or record.last_options
    stations = fetch_all_stations()
    target_id = trip_state.get("target_station_id", "")
    target_station = stations.get(target_id, {})
    return {
        "type": "status",
        "monitor_status": record.status,
        "target_station_id": target_id,
        "target_station_name": trip_state.get("target_station_name", ""),
        "target_lat": target_station.get("lat"),
        "target_lon": target_station.get("lon"),
        "docks": (trip_state.get("dock_history") or [{}])[-1].get("docks_available"),
        "ebikes": _latest_target_ebikes(trip_state),
        "message": record.last_message,
        "candidates": _attach_coordinates(record.last_candidates, stations),
        "options": _attach_coordinates(options[:3], stations),
        "awaiting_new_target": record.awaiting_new_target,
    }


def build_alert_spoken_message(alert: dict[str, Any]) -> str:
    headline = alert.get("headline", "")
    message = alert.get("message", "")
    alternatives = alert.get("alternatives", [])
    parts = [part for part in [headline, message] if part]
    if alternatives:
        option_parts = []
        for index, option in enumerate(alternatives[:3], start=1):
            name = option.get("station_name") or option.get("name", "nearby station")
            docks = option.get("docks_available", 0)
            option_parts.append(f"option {index}: {name} with {docks} docks")
        parts.append(" ".join(option_parts))
    return " ".join(parts) or "Dock availability has changed."
