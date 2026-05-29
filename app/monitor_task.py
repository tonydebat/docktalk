"""Background monitor loop for the live voice app."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from starlette.websockets import WebSocketState

from app.live_tools import (
    build_alert_spoken_message,
    format_status_payload,
    run_background_monitor_tick,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 15
MAX_STALE_MINUTES = 10


async def run_monitor(
    session_id: str,
    sessions: dict[str, Any],
    *,
    poll_interval_seconds: int = POLL_INTERVAL_SECONDS,
    max_stale_minutes: int = MAX_STALE_MINUTES,
) -> None:
    logger.info("[%s] Monitor started", session_id)
    last_successful_tick_at: datetime | None = None
    last_alert_signature: str | None = None

    while True:
        record = sessions.get(session_id)
        if record is None:
            logger.info("[%s] Session missing; monitor exiting", session_id)
            return

        trip_state = record.trip_state
        if trip_state is None:
            await asyncio.sleep(poll_interval_seconds)
            continue

        if record.status == "STOPPED" or trip_state.get("status") == "finished":
            logger.info("[%s] Session stopped; monitor exiting", session_id)
            return

        if last_successful_tick_at is not None:
            if datetime.now() - last_successful_tick_at > timedelta(minutes=max_stale_minutes):
                record.status = "STALE_DATA"
                await record.alert_queue.put(
                    "VERBATIM_ALERT: Live dock data has not refreshed recently. Monitoring is paused."
                )
                await _push_status_to_browser(record)
                return

        if trip_state.get("status") in {"alerted", "check_in"}:
            await _push_status_to_browser(record)
            await asyncio.sleep(poll_interval_seconds)
            continue

        next_check_at = trip_state.get("next_check_at")
        if isinstance(next_check_at, datetime) and datetime.now() < next_check_at:
            await asyncio.sleep(min(5, poll_interval_seconds))
            continue

        try:
            await asyncio.get_running_loop().run_in_executor(
                None,
                run_background_monitor_tick,
                record,
            )
            last_successful_tick_at = datetime.now()
        except Exception as exc:
            logger.warning("[%s] Monitor tick failed: %s", session_id, exc)
            await asyncio.sleep(poll_interval_seconds)
            continue

        alert = trip_state.get("alert")
        if alert:
            signature = f"{alert.get('headline', '')}|{alert.get('message', '')}"
            if signature != last_alert_signature:
                await record.alert_queue.put(
                    f"VERBATIM_ALERT: {build_alert_spoken_message(alert)}"
                )
                last_alert_signature = signature

        check_in = trip_state.get("check_in")
        if check_in:
            await record.alert_queue.put(
                f"VERBATIM_ALERT: {check_in.get('message', 'Are you still riding?')}"
            )

        await _push_status_to_browser(record)
        await asyncio.sleep(poll_interval_seconds)


async def _push_status_to_browser(record: Any) -> None:
    ws = record.ws_ref
    if ws is None or ws.client_state == WebSocketState.DISCONNECTED:
        return
    try:
        await ws.send_text(json.dumps(format_status_payload(record)))
    except Exception:
        return
