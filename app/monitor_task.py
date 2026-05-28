"""Background asyncio monitor loop.

Polls GBFS, evaluates risk with run_tick(), and pushes spoken alerts
to the session's alert_queue for live_bridge to inject into the Gemini
Live session.

The monitor runs independently of the WebSocket — it survives disconnects
and self-terminates when the session reaches STOPPED status or GBFS data
goes stale for more than max_stale_minutes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from src.bikeshare.agent import run_monitor_tick
from src.bikeshare.station_data import fetch_live_status, get_station_status
from src.bikeshare.trip_state import record_tick_decision

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS = 60
QUICK_RETRY_SECONDS = 15
MAX_QUICK_RETRIES = 3
WARNING_COOLDOWN_SECONDS = 180
MAX_STALE_MINUTES = 10


async def run_monitor(
    session_id: str,
    sessions: dict[str, Any],
    *,
    poll_interval_seconds: int = POLL_INTERVAL_SECONDS,
    quick_retry_seconds: int = QUICK_RETRY_SECONDS,
    max_quick_retries: int = MAX_QUICK_RETRIES,
    warning_cooldown_seconds: int = WARNING_COOLDOWN_SECONDS,
    max_stale_minutes: int = MAX_STALE_MINUTES,
) -> None:
    """Monitor dock availability for the session's target station.

    Args:
        session_id: The session to monitor.
        sessions: Shared session dict from FastAPI app state.
        poll_interval_seconds: Normal polling cadence.
        quick_retry_seconds: Retry cadence on fetch failure.
        max_quick_retries: Max retries before treating data as stale.
        warning_cooldown_seconds: Minimum seconds between repeated alerts.
        max_stale_minutes: Stop monitoring if no fresh data for this long.
    """
    logger.info("[%s] Monitor started", session_id)

    last_spoken_alert_type: str | None = None
    last_spoken_at: datetime | None = None
    last_successful_fetch_at: datetime | None = None
    consecutive_failures = 0

    while True:
        record = sessions.get(session_id)
        if record is None:
            logger.info("[%s] Session gone; monitor exiting", session_id)
            return

        trip_state = record.trip_state
        if trip_state is None:
            await asyncio.sleep(QUICK_RETRY_SECONDS)
            continue

        # Self-terminate if the session has ended
        status = trip_state.get("status", "monitoring")
        if status in ("finished", "STOPPED") or record.status == "STOPPED":
            logger.info("[%s] Session stopped; monitor exiting", session_id)
            return

        # Stale data check
        if last_successful_fetch_at is not None:
            stale_threshold = timedelta(minutes=max_stale_minutes)
            if datetime.now() - last_successful_fetch_at > stale_threshold:
                logger.warning("[%s] GBFS stale >%dm; stopping monitor", session_id, max_stale_minutes)
                await record.alert_queue.put(
                    "VERBATIM_ALERT: Dock data hasn't refreshed in a while. "
                    "I'm pausing monitoring — please confirm your destination when you're ready."
                )
                return

        # ── Fetch live status ────────────────────────────────────────────────
        station_id: str = trip_state.get("target_station_id", "")
        try:
            loop = asyncio.get_running_loop()
            live_status = await loop.run_in_executor(None, fetch_live_status)
            consecutive_failures = 0
            last_successful_fetch_at = datetime.now()

            station_data = live_status.get(station_id, {})
            _ = station_data  # stale-data tracking only; observation is handled by run_monitor_tick

        except Exception as exc:
            consecutive_failures += 1
            logger.warning("[%s] GBFS fetch failed (%d): %s", session_id, consecutive_failures, exc)

            if consecutive_failures >= max_quick_retries:
                await asyncio.sleep(poll_interval_seconds)
            else:
                await asyncio.sleep(quick_retry_seconds)
            continue

        # ── Risk evaluation ──────────────────────────────────────────────────
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None, run_monitor_tick, trip_state
            )
            record_tick_decision(trip_state)
        except Exception as exc:
            logger.warning("[%s] run_monitor_tick failed: %s", session_id, exc)
            await asyncio.sleep(poll_interval_seconds)
            continue

        # ── Alert decision ───────────────────────────────────────────────────
        alert = trip_state.get("alert")
        should_speak = _should_speak_alert(
            alert=alert,
            last_spoken_alert_type=last_spoken_alert_type,
            last_spoken_at=last_spoken_at,
            cooldown_seconds=warning_cooldown_seconds,
        )

        if should_speak and alert:
            spoken = _build_spoken_message(alert)
            await record.alert_queue.put(f"VERBATIM_ALERT: {spoken}")
            last_spoken_alert_type = alert.get("type", "warning")
            last_spoken_at = datetime.now()
            logger.info("[%s] Alert queued: %s", session_id, last_spoken_alert_type)

        # Update session-level status from trip state and push to browser
        ts_status = trip_state.get("status", "monitoring")
        if ts_status in ("finished", "STOPPED"):
            record.status = "STOPPED"
            await _push_status_to_browser(record, trip_state)
            return
        record.status = _derive_record_status(trip_state)
        await _push_status_to_browser(record, trip_state)

        await asyncio.sleep(poll_interval_seconds)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _should_speak_alert(
    alert: dict | None,
    last_spoken_alert_type: str | None,
    last_spoken_at: datetime | None,
    cooldown_seconds: int,
) -> bool:
    """Return True when the alert should be spoken aloud."""
    if not alert:
        return False

    alert_type = alert.get("type", "warning")

    # Critical alerts always break through
    if alert_type == "critical":
        return True

    # Apply cooldown for repeated alerts of the same type
    if (
        alert_type == last_spoken_alert_type
        and last_spoken_at is not None
        and (datetime.now() - last_spoken_at).total_seconds() < cooldown_seconds
    ):
        return False

    return True


def _build_spoken_message(alert: dict) -> str:
    """Build the spoken message from an alert dict."""
    headline = alert.get("headline", "")
    message = alert.get("message", "")
    alternatives = alert.get("alternatives", [])

    parts = []
    if headline:
        parts.append(headline)
    if message:
        parts.append(message)

    if alternatives:
        alt_phrases = [
            f"{a.get('name', '')} with {a.get('available_docks', 0)} docks"
            for a in alternatives[:3]
        ]
        if len(alt_phrases) == 1:
            parts.append(f"Your best option is {alt_phrases[0]}.")
        else:
            parts.append(f"Your options are {', '.join(alt_phrases[:-1])}, and {alt_phrases[-1]}.")

    return " ".join(parts) or "Dock availability has changed."


def _derive_record_status(trip_state: dict[str, Any]) -> str:
    """Map trip_state fields to a status string for record.status and the browser card."""
    ts_status = trip_state.get("status", "monitoring")
    if ts_status in ("finished", "STOPPED"):
        return "STOPPED"
    if trip_state.get("alert"):
        return "ALERTED"
    latest = (trip_state.get("dock_history") or [{}])[-1]
    docks = latest.get("docks_available")
    if docks is not None:
        if docks <= 2:
            return "MONITORING_WARNING"
        if docks <= 5:
            return "MONITORING_WATCH"
    return "MONITORING_SAFE"


async def _push_status_to_browser(record: Any, trip_state: dict[str, Any]) -> None:
    """Send a status event to the browser via the stored WebSocket reference."""
    ws = record.ws_ref
    if ws is None:
        return
    try:
        from starlette.websockets import WebSocketState
        if ws.client_state == WebSocketState.DISCONNECTED:
            return
        payload = {
            "type": "status",
            "monitor_status": record.status,
            "target_station_id": trip_state.get("target_station_id", ""),
            "target_station_name": trip_state.get("target_station_name", ""),
            "docks": (trip_state.get("dock_history") or [{}])[-1].get("docks_available", None),
        }
        await ws.send_text(json.dumps(payload))
    except Exception:
        pass
