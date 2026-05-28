"""Gemini Live bridge — the WebSocket ↔ Gemini Live session proxy.

This module owns the three concurrent tasks that make up the voice pipeline
for one rider session:

  Task A (browser → Live):
    Reads binary PCM16 frames from the browser WebSocket and forwards them
    as audio blobs to the Gemini Live session.

  Task B (Live → browser):
    Reads LiveServerMessages from Gemini. Streams audio parts back to the
    browser as binary frames. Dispatches tool calls to app/tools.py and
    returns FunctionResponses. Sends JSON status events to the browser.

  Task C (alert_queue → Live):
    Watches for VERBATIM_ALERT messages from the monitor task and injects
    them into the Gemini Live session as client-side content turns.

On disconnect, only task A ends; B and C keep running until the monitor
asks them to stop (ADR-0003: monitor survives disconnects).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from google.genai import Client
from google.genai import types as gtypes
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState
from websockets.exceptions import ConnectionClosedError

from app.monitor_task import run_monitor
from app.session_store import SessionRecord
from app.tools import GEMINI_TOOLS, dispatch

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_PATH = (
    Path(__file__).parent.parent / "docktalk" / "agent" / "prompts" / "live_system.md"
)
_LIVE_MODEL = "gemini-3.1-flash-live-preview"

# Log 1 in every N audio frames to avoid flooding at 60fps
_AUDIO_LOG_EVERY_N_FRAMES = 30


def _load_system_prompt() -> str:
    return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _make_live_config() -> gtypes.LiveConnectConfig:
    return gtypes.LiveConnectConfig(
        system_instruction=gtypes.Content(
            parts=[gtypes.Part(text=_load_system_prompt())]
        ),
        tools=GEMINI_TOOLS,
        response_modalities=["AUDIO"],
        speech_config=gtypes.SpeechConfig(
            voice_config=gtypes.VoiceConfig(
                prebuilt_voice_config=gtypes.PrebuiltVoiceConfig(voice_name="Charon")
            )
        ),
        # Push-to-talk: disable automatic VAD so the model waits for explicit
        # ActivityStart / ActivityEnd signals sent by the browser button.
        # Without this, Gemini waits 1-3 s of silence before responding.
        realtime_input_config=gtypes.RealtimeInputConfig(
            automatic_activity_detection=gtypes.AutomaticActivityDetection(
                disabled=True
            )
        ),
        # Transcription: echo what the rider says and what the model says
        # as text so both sides of the conversation appear in the server log.
        input_audio_transcription=gtypes.AudioTranscriptionConfig(),
        output_audio_transcription=gtypes.AudioTranscriptionConfig(),
    )


async def run_bridge(
    websocket: WebSocket,
    session_id: str,
    sessions: dict[str, SessionRecord],
) -> None:
    """Run the Gemini Live bridge for one rider session.

    Called by server.py when a WebSocket connects. Manages the full
    session lifecycle: connect to Gemini Live, relay audio, dispatch tools,
    inject monitor alerts, and spawn/restore the background monitor.

    On browser disconnect, the Gemini Live session is torn down but the
    monitor loop in ``sessions`` continues running.
    """
    record = sessions.setdefault(session_id, SessionRecord(session_id=session_id))
    record.ws_ref = websocket

    # Guard: if this session was explicitly stopped (rider said "cancel" /
    # "I returned the bike"), tell the browser and exit without opening a new
    # Gemini Live session.  Without this, the browser's auto-reconnect loop
    # would spin up a fresh Gemini connection every 3 s indefinitely.
    if record.status == "STOPPED":
        logger.info("[%s] Reconnect on stopped session; sending session_ended", session_id)
        await _send_session_ended(websocket)
        return

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        logger.error("[%s] GEMINI_API_KEY is not set — connection will fail", session_id)
    else:
        logger.info("[%s] Using API key: %s…%s", session_id, api_key[:6], api_key[-4:])
    logger.info("[%s] Connecting to Gemini Live model: %s (v1alpha)", session_id, _LIVE_MODEL)
    client = Client(
        api_key=api_key,
        http_options=gtypes.HttpOptions(api_version="v1alpha"),
    )

    async with client.aio.live.connect(
        model=_LIVE_MODEL, config=_make_live_config()
    ) as live_session:
        logger.info("[%s] Gemini Live session open", session_id)

        # The SDK already consumed setup_complete before yielding the session.
        # Mark ready immediately so the audio relay task can start.
        gemini_ready = asyncio.Event()
        gemini_ready.set()
        logger.info("[%s] Audio relay unblocked (SDK handled setup_complete)", session_id)

        # Push current session state to the browser immediately so the status
        # card is correct on both the initial connect and every reconnect.
        # Without this the card stays on "No station selected / NOT STARTED"
        # after the bridge reconnects mid-session.
        await _send_status_event(websocket, record)

        # Restore state on reconnect
        if record.trip_state and record.status not in ("NOT_STARTED", "STOPPED"):
            await _inject_reconnect_snapshot(live_session, record)

        browser_to_live_task = asyncio.create_task(
            _task_browser_to_live(websocket, live_session),
            name=f"browser-to-live-{session_id}",
        )
        live_to_browser_task = asyncio.create_task(
            _task_live_to_browser(websocket, live_session, session_id, sessions),
            name=f"live-to-browser-{session_id}",
        )
        # alert_task is managed separately: it must not govern bridge lifetime.
        # If status reaches STOPPED while Gemini is still speaking a farewell,
        # _task_alert_injection exits immediately — we don't want that to tear
        # down the bridge mid-sentence.  It is always cancelled in the finally.
        alert_task = asyncio.create_task(
            _task_alert_injection(live_session, record),
            name=f"alert-inject-{session_id}",
        )

        try:
            # Only the two I/O tasks drive the bridge lifecycle.
            # FIRST_COMPLETED fires when the browser disconnects (browser_to_live
            # returns) OR when the Gemini session closes (live_to_browser returns
            # or raises).  Either event is a clean reason to tear down.
            done, pending = await asyncio.wait(
                [browser_to_live_task, live_to_browser_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            for t in done:
                exc = t.exception()
                if exc:
                    raise exc
        except WebSocketDisconnect:
            logger.info("[%s] Browser disconnected; keeping monitor alive", session_id)
        except (ConnectionClosedError, Exception) as exc:
            logger.warning("[%s] Bridge ended: %s", session_id, exc)
        finally:
            alert_task.cancel()
            try:
                await alert_task
            except (asyncio.CancelledError, Exception):
                pass
            record.ws_ref = None
            logger.info("[%s] Bridge torn down", session_id)


# ── Task A: browser → Live ────────────────────────────────────────────────────


async def _task_browser_to_live(
    websocket: WebSocket,
    live_session,
) -> None:
    """Forward PCM16 audio and push-to-talk activity signals to Gemini Live.

    Binary frames are audio (PCM16 at 16 kHz).
    Text frames are JSON control messages:
      {"type": "start_of_speech"}  → ActivityStart  (button pressed)
      {"type": "end_of_speech"}    → ActivityEnd    (button released)

    With automatic VAD disabled, Gemini only processes audio between an
    ActivityStart and an ActivityEnd, so it responds the instant the rider
    releases the button rather than waiting for a silence timeout.
    """
    frames_sent = 0
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break

            # ── Audio frame ───────────────────────────────────────────────
            if "bytes" in message and message["bytes"]:
                audio_blob = gtypes.Blob(
                    data=message["bytes"], mime_type="audio/pcm;rate=16000"
                )
                await live_session.send_realtime_input(audio=audio_blob)
                frames_sent += 1
                if frames_sent % 50 == 1:
                    logger.debug("Browser→Live: %d frames sent", frames_sent)

            # ── Control frame ─────────────────────────────────────────────
            elif "text" in message and message["text"]:
                try:
                    ctrl = json.loads(message["text"])
                except (ValueError, TypeError):
                    continue
                ctrl_type = ctrl.get("type")
                if ctrl_type == "start_of_speech":
                    await live_session.send_realtime_input(
                        activity_start=gtypes.ActivityStart()
                    )
                    logger.debug("Sent ActivityStart to Gemini Live")
                elif ctrl_type == "end_of_speech":
                    await live_session.send_realtime_input(
                        activity_end=gtypes.ActivityEnd()
                    )
                    logger.debug("Sent ActivityEnd to Gemini Live")

    except (WebSocketDisconnect, ConnectionClosedError):
        pass
    logger.info("Browser audio relay ended (%d frames sent)", frames_sent)


# ── Task B: Live → browser ────────────────────────────────────────────────────


async def _task_live_to_browser(
    websocket: WebSocket,
    live_session,
    session_id: str,
    sessions: dict[str, SessionRecord],
) -> None:
    """Stream Gemini Live output to the browser and dispatch tool calls."""
    record = sessions[session_id]
    audio_frame_count = 0
    output_transcript_buffer: list[str] = []  # accumulate model speech text across chunks

    # The SDK's receive() generator exhausts after each turn_complete.  Wrap
    # it in a while loop so the bridge stays alive across multiple turns.
    # Without this wrapper the bridge tears down after every model response,
    # causing a 3-second reconnect cycle and losing all conversation context.
    while websocket.client_state != WebSocketState.DISCONNECTED:
        async for message in live_session.receive():
            if websocket.client_state == WebSocketState.DISCONNECTED:
                return

            # Audio from model → binary frame to browser
            server_content = getattr(message, "server_content", None)
            if server_content:
                model_turn = getattr(server_content, "model_turn", None)
                if model_turn and model_turn.parts:
                    for part in model_turn.parts:
                        # Text part — always log so developer can see what the model says
                        text = getattr(part, "text", None)
                        if text:
                            logger.info("[%s] MODEL TEXT: %s", session_id, text.strip())

                        # Audio part → forward to browser as binary frame
                        inline = getattr(part, "inline_data", None)
                        if inline and inline.data:
                            audio_frame_count += 1
                            if audio_frame_count % _AUDIO_LOG_EVERY_N_FRAMES == 1:
                                logger.debug(
                                    "[%s] Audio frame #%d (%d bytes)",
                                    session_id, audio_frame_count, len(inline.data),
                                )
                            await websocket.send_bytes(inline.data)

                # Input transcription — what the rider said.
                # The API sends the complete transcript as a single chunk (finished=None).
                # Log any non-empty text; don't gate on finished=True.
                input_tx = getattr(server_content, "input_transcription", None)
                if input_tx:
                    tx_text = getattr(input_tx, "text", None)
                    if tx_text and tx_text.strip():
                        logger.info("[%s] RIDER: %s", session_id, tx_text.strip())

                # Output transcription — what the model is saying.
                # The API streams text chunks incrementally (finished=None throughout).
                # Accumulate here; emit the full sentence on turn_complete below.
                output_tx = getattr(server_content, "output_transcription", None)
                if output_tx:
                    tx_text = getattr(output_tx, "text", None)
                    if tx_text and tx_text.strip():
                        output_transcript_buffer.append(tx_text)

                turn_complete = getattr(server_content, "turn_complete", False)
                if turn_complete:
                    if output_transcript_buffer:
                        logger.info(
                            "[%s] AGENT: %s",
                            session_id,
                            "".join(output_transcript_buffer).strip(),
                        )
                        output_transcript_buffer.clear()
                    logger.info("[%s] Model turn complete (sent %d audio frames)", session_id, audio_frame_count)
                    audio_frame_count = 0

            # Tool call → dispatch → FunctionResponse
            tool_call = getattr(message, "tool_call", None)
            if tool_call and tool_call.function_calls:
                function_responses = []
                for fc in tool_call.function_calls:
                    args = dict(fc.args) if fc.args else {}
                    logger.info("[%s] TOOL CALL: %s(%s)", session_id, fc.name, args)
                    result = await dispatch(fc.name, args, record)
                    logger.info("[%s] TOOL RESULT: %s → %s", session_id, fc.name, result)

                    # Spawn monitor after confirm_station sets spawn_monitor flag
                    if fc.name == "confirm_station" and record.spawn_monitor:
                        record.spawn_monitor = False
                        await _maybe_start_monitor(session_id, sessions)

                    function_responses.append(
                        gtypes.FunctionResponse(name=fc.name, id=fc.id, response=result)
                    )

                await live_session.send_tool_response(function_responses=function_responses)

                # Push a status event to the browser DOM
                await _send_status_event(websocket, record)

            # go_away means the server is shutting down this session
            go_away = getattr(message, "go_away", None)
            if go_away is not None:
                logger.warning("[%s] Server sent go_away: %s", session_id, go_away)
                return


# ── Task C: alert_queue → Live ────────────────────────────────────────────────


async def _task_alert_injection(live_session, record: SessionRecord) -> None:
    """Inject VERBATIM_ALERT messages from the monitor into the Live session."""
    while record.status != "STOPPED":
        try:
            spoken: str = await asyncio.wait_for(record.alert_queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            continue

        try:
            await live_session.send_client_content(
                turns=[
                    gtypes.Content(
                        role="user",
                        parts=[gtypes.Part(text=spoken)],
                    )
                ],
                turn_complete=True,
            )
            logger.info("Alert injected: %.80s", spoken)
        except (ConnectionClosedError, Exception) as exc:
            logger.warning("Alert injection failed: %s", exc)


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _send_session_ended(websocket: WebSocket) -> None:
    """Tell the browser that the session has ended and close the WebSocket."""
    try:
        await websocket.send_text(json.dumps({"type": "session_ended"}))
        await websocket.close(code=1000)
    except Exception:
        pass


async def _maybe_start_monitor(
    session_id: str, sessions: dict[str, SessionRecord]
) -> None:
    """Spawn the background monitor if one is not already running."""
    record = sessions[session_id]
    if record.monitor_task_handle and not record.monitor_task_handle.done():
        return
    record.monitor_task_handle = asyncio.create_task(
        run_monitor(session_id, sessions), name=f"monitor-{session_id}"
    )
    logger.info("[%s] Monitor task spawned", session_id)


async def _inject_reconnect_snapshot(live_session, record: SessionRecord) -> None:
    """Inject a reconnect snapshot so the rider is re-oriented after reconnecting."""
    ts = record.trip_state
    if not ts:
        return
    name = ts.get("target_station_name", "your target station")
    docks = "unknown"
    history = ts.get("dock_history", [])
    if history:
        docks = str(history[-1].get("docks_available", "unknown"))

    snapshot = (
        f"VERBATIM_ALERT: Welcome back. Monitoring {name}. "
        f"Last known docks: {docks}."
    )
    await live_session.send_client_content(
        turns=[gtypes.Content(role="user", parts=[gtypes.Part(text=snapshot)])],
        turn_complete=True,
    )


async def _send_status_event(websocket: WebSocket, record: SessionRecord) -> None:
    """Push a JSON status event to the browser."""
    if websocket.client_state == WebSocketState.DISCONNECTED:
        return
    ts = record.trip_state or {}
    payload = {
        "type": "status",
        "monitor_status": record.status,
        "target_station_id": ts.get("target_station_id", ""),
        "target_station_name": ts.get("target_station_name", ""),
        "docks": (ts.get("dock_history") or [{}])[-1].get("docks_available", None),
    }
    try:
        await websocket.send_text(json.dumps(payload))
    except Exception:
        pass
