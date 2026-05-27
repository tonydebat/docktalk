# Beginner notes:
# This file is the v1 fill-probability predictor. It is rule-based: no learning.
# It estimates how likely the destination station will be full at the rider's
# arrival moment, using station_context.csv plus simple time-of-day drift tables.
#
# Why a rule-based v1: it is explainable and demo-safe. Later we can replace
# _drift_for() with a direct lookup into live/historical station trends without
# changing the tool contract.
#
# Run the self-tests with: python src/bikeshare/predictor.py

import csv
import re
from datetime import datetime, time
from pathlib import Path
from typing import Any


# Drift = expected change in docks_available per minute.
# Sign convention:
#   POSITIVE drift -> docks_available is INCREASING (bikes being taken out).
#                     Typical at morning rush (people grab bikes to go to work).
#   NEGATIVE drift -> docks_available is DECREASING (bikes being returned, station filling up with bikes).
#                     Typical at evening rush, especially at transit hubs like Union.
# Magnitudes scale with tier. Tune these once you have a few days of real GBFS data.
DRIFT_TABLE: dict[tuple[str, str], float] = {
    # (tier, hour_band): drift per minute
    ("very_high", "morning_rush_weekday"): +0.30,
    ("very_high", "evening_rush_weekday"): -0.40,
    ("very_high", "midday_weekday"):       -0.05,
    ("very_high", "evening_weekday"):      -0.10,
    ("very_high", "weekend_day"):          -0.10,
    ("very_high", "night"):                +0.02,

    ("high", "morning_rush_weekday"): +0.18,
    ("high", "evening_rush_weekday"): -0.25,
    ("high", "midday_weekday"):       -0.03,
    ("high", "evening_weekday"):      -0.05,
    ("high", "weekend_day"):          -0.05,
    ("high", "night"):                +0.01,

    ("medium", "morning_rush_weekday"): +0.08,
    ("medium", "evening_rush_weekday"): -0.12,
    ("medium", "midday_weekday"):       -0.01,
    ("medium", "evening_weekday"):      -0.02,
    ("medium", "weekend_day"):          -0.03,
    ("medium", "night"):                 0.00,

    ("low", "morning_rush_weekday"): +0.03,
    ("low", "evening_rush_weekday"): -0.05,
    ("low", "midday_weekday"):        0.00,
    ("low", "evening_weekday"):      -0.01,
    ("low", "weekend_day"):          -0.01,
    ("low", "night"):                 0.00,
}


# Optional manual overrides for demo-specific tuning.
# Most station tiers should now come from data/station_context.csv.
STATION_TIERS: dict[str, str] = {
}

STATION_CONTEXT_PATH = Path(__file__).resolve().parents[2] / "data" / "station_context.csv"
_profile_cache: dict[str, dict[str, str]] | None = None

_TIER_LEVELS = ["low", "medium", "high", "very_high"]
_RISK_SCORE = {"unknown": 1, "low": 0, "medium": 2, "high": 3}
_USAGE_SCORE = {"unknown": 0, "low": 0, "medium": 1, "high": 2, "very_high": 3}


def classify_hour_band(now: datetime) -> str:
    """Map a real datetime to one of our hour bands."""
    is_weekend = now.weekday() >= 5
    hour = now.hour

    if is_weekend:
        if 22 <= hour or hour < 6:
            return "night"
        return "weekend_day"

    if 7 <= hour < 10:
        return "morning_rush_weekday"
    if 16 <= hour < 19:
        return "evening_rush_weekday"
    if 10 <= hour < 16:
        return "midday_weekday"
    if 19 <= hour < 22:
        return "evening_weekday"
    return "night"


def _drift_for(tier: str, hour_band: str) -> float:
    return DRIFT_TABLE.get((tier, hour_band), 0.0)


def _load_station_profiles() -> dict[str, dict[str, str]]:
    global _profile_cache
    if _profile_cache is not None:
        return _profile_cache
    if not STATION_CONTEXT_PATH.exists():
        _profile_cache = {}
        return _profile_cache
    with STATION_CONTEXT_PATH.open(newline="", encoding="utf-8") as f:
        _profile_cache = {row["station_id"]: row for row in csv.DictReader(f)}
    return _profile_cache


def _is_true(value: Any) -> bool:
    return str(value).strip().lower() == "true"


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _clamp_tier_score(score: int) -> int:
    return max(0, min(score, len(_TIER_LEVELS) - 1))


def _tier_from_profile(profile: dict[str, str] | None) -> str:
    if not profile:
        return "medium"

    return_risk = profile.get("historical_return_risk_class", "unknown")
    usage = profile.get("historical_usage_class", "unknown")
    area_type = profile.get("primary_area_type", "unknown")
    capacity_class = profile.get("capacity_class", "medium")

    score = max(_RISK_SCORE.get(return_risk, 1), _USAGE_SCORE.get(usage, 0) - 1)

    if area_type in {"transit_hub", "tourist_event", "waterfront"} and usage in {"high", "very_high"}:
        score += 1
    if capacity_class in {"tiny", "small"} and usage in {"high", "very_high"}:
        score += 1
    if _is_true(profile.get("is_superstation")) or profile.get("valet_hours"):
        score = max(score, 2)

    return _TIER_LEVELS[_clamp_tier_score(score)]


def _parse_clock(value: str) -> time:
    return datetime.strptime(value.strip(), "%I:%M%p").time()


def _day_group_matches(valet_hours: str, now: datetime) -> bool:
    text = valet_hours.lower()
    weekday = now.weekday()
    if "mon-fri" in text:
        return weekday < 5
    if "fri-sun" in text:
        return weekday in {4, 5, 6}
    return True


def _valet_status(valet_hours: str, now: datetime) -> str:
    if not valet_hours:
        return "none"

    lowered = valet_hours.lower()
    if "event" in lowered or "game" in lowered or "pre-show" in lowered:
        return "conditional"

    if not _day_group_matches(valet_hours, now):
        return "inactive"

    intervals = re.findall(r"(\d{1,2}:\d{2}[AP]M)-(\d{1,2}:\d{2}[AP]M)", valet_hours)
    if not intervals:
        return "conditional"

    current = now.time()
    for start_raw, end_raw in intervals:
        start = _parse_clock(start_raw)
        end = _parse_clock(end_raw)
        if start <= current <= end:
            return "active"
    return "inactive"


def _adjust_drift_for_profile(
    *,
    drift: float,
    profile: dict[str, str] | None,
    hour_band: str,
    now: datetime,
) -> tuple[float, list[str], str]:
    if not profile:
        return drift, ["no station_context profile"], "none"

    notes: list[str] = []
    adjusted = drift
    area_type = profile.get("primary_area_type", "unknown")
    return_risk = profile.get("historical_return_risk_class", "unknown")
    usage = profile.get("historical_usage_class", "unknown")
    net_flow = _optional_float(profile.get("historical_net_flow_per_day"))
    valet_status = _valet_status(profile.get("valet_hours", ""), now)

    if return_risk == "high":
        adjusted -= 0.05
        notes.append("high historical return risk")
    elif return_risk == "low":
        adjusted += 0.03
        notes.append("low historical return risk")

    if net_flow is not None and net_flow > 10:
        adjusted -= 0.04
        notes.append("positive historical net arrivals")
    elif net_flow is not None and net_flow < -10:
        adjusted += 0.03
        notes.append("historically drains bikes away")

    if area_type == "transit_hub" and hour_band == "evening_rush_weekday":
        adjusted -= 0.06
        notes.append("evening rush at transit hub")
    if area_type in {"tourist_event", "waterfront"} and hour_band == "weekend_day":
        adjusted -= 0.05
        notes.append("weekend tourist/waterfront demand")
    if area_type == "residential" and hour_band == "morning_rush_weekday":
        adjusted -= 0.03
        notes.append("morning residential return pressure")

    if usage == "very_high" and adjusted < 0:
        adjusted *= 1.15
        notes.append("very high station usage")

    if valet_status == "active":
        adjusted = max(adjusted, 0.08)
        notes.append("active valet hours reduce return risk")
    elif valet_status == "conditional":
        notes.append("valet is event-based or conditional")

    return adjusted, notes, valet_status


def _docks_to_risk(predicted_docks: float) -> float:
    """Map a predicted dock count to probability the station has < 2 docks."""
    if predicted_docks < 0.5:
        return 0.90
    if predicted_docks < 1.5:
        return 0.70
    if predicted_docks < 3.0:
        return 0.45
    if predicted_docks < 5.0:
        return 0.20
    return 0.05


def predict_fill_probability(
    *,
    station_id: str,
    minutes_ahead: int,
    current_docks: int,
    capacity: int,
    now: datetime,
) -> dict[str, Any]:
    """Estimate probability that station has fewer than 2 docks at `minutes_ahead`."""
    profile = _load_station_profiles().get(station_id)
    tier = STATION_TIERS.get(station_id) or _tier_from_profile(profile)
    hour_band = classify_hour_band(now)
    base_drift = _drift_for(tier, hour_band)
    drift, adjustment_notes, valet_status = _adjust_drift_for_profile(
        drift=base_drift,
        profile=profile,
        hour_band=hour_band,
        now=now,
    )

    projected = current_docks + drift * minutes_ahead
    projected = max(0.0, min(float(capacity), projected))

    p_under_2 = _docks_to_risk(projected)

    return {
        "p_under_2_docks": round(p_under_2, 2),
        "predicted_docks": round(projected, 1),
        "basis": (
            f"tier={tier}, band={hour_band}, base_drift={base_drift:+.2f}/min, "
            f"adjusted_drift={drift:+.2f}/min"
        ),
        "confidence": "medium" if profile else "low",
        "station_context": {
            "station_name": profile.get("station_name") if profile else "",
            "primary_area_type": profile.get("primary_area_type") if profile else "unknown",
            "historical_usage_class": profile.get("historical_usage_class") if profile else "unknown",
            "historical_return_risk_class": (
                profile.get("historical_return_risk_class") if profile else "unknown"
            ),
            "valet_hours": profile.get("valet_hours", "") if profile else "",
            "valet_status": valet_status,
            "adjustments": adjustment_notes,
        },
    }


def main() -> None:
    # Scenario 1: high-pressure downtown station, evening rush, 4 docks, ETA 10 min.
    risk_eta_10 = predict_fill_probability(
        station_id="7015",
        minutes_ahead=10,
        current_docks=4,
        capacity=39,
        now=datetime(2026, 5, 27, 17, 30),  # Wednesday 5:30 PM
    )
    assert risk_eta_10["p_under_2_docks"] <= 0.2, risk_eta_10
    assert risk_eta_10["station_context"]["valet_status"] == "active"
    print("Scenario 1 (active valet, rush, ETA 10):", risk_eta_10)

    # Scenario 2: same station outside valet hours. It should be riskier.
    no_valet = predict_fill_probability(
        station_id="7015",
        minutes_ahead=10,
        current_docks=4,
        capacity=39,
        now=datetime(2026, 5, 30, 10, 0),  # Saturday 10 AM
    )
    assert no_valet["p_under_2_docks"] >= risk_eta_10["p_under_2_docks"]
    print("Scenario 2 (same station, no valet):", no_valet)

    # Scenario 3: quiet station, plenty of docks. Should be low risk.
    quiet = predict_fill_probability(
        station_id="UNKNOWN_QUIET",
        minutes_ahead=20,
        current_docks=12,
        capacity=20,
        now=datetime(2026, 5, 27, 17, 30),
    )
    assert quiet["p_under_2_docks"] <= 0.2
    print("Scenario 3 (quiet station):", quiet)


if __name__ == "__main__":
    main()
