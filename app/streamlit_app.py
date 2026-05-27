"""DockTalk Monitor Debugger — Streamlit app.

Single-page debugger for the DockTalk monitor loop. Pick a destination
station, set an ETA, and fire ticks one at a time to watch the agent
observe dock counts, call tools, and decide what to do next.
"""

import copy
import hashlib
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_dotenv = find_dotenv()
if _dotenv:
    load_dotenv(_dotenv)

import streamlit as st
import streamlit.components.v1 as components

# ── Logging ────────────────────────────────────────────────────────────────────
# Voice-input debug log. Always prints to the terminal where you ran
# `streamlit run`. Set DOCKTALK_VOICE_DEBUG=1 in your environment to also dump
# every recorded audio blob to DOCKTALK_DEBUG_DIR (default: /tmp/docktalk_debug)
# so you can replay them with `ffplay` / `afplay` / Whisper directly.
_log = logging.getLogger("docktalk.voice")
if not _log.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s [docktalk.voice] %(levelname)s: %(message)s"
    ))
    _log.addHandler(_handler)
_log.setLevel(logging.DEBUG if os.getenv("DOCKTALK_VOICE_DEBUG") else logging.INFO)

VOICE_DEBUG = bool(os.getenv("DOCKTALK_VOICE_DEBUG"))
DEBUG_DIR = Path(os.getenv("DOCKTALK_DEBUG_DIR") or "/tmp/docktalk_debug")

from src.bikeshare.agent import run_tick
from src.bikeshare.destination_resolver import merge_info_and_status, resolve_destination
from src.bikeshare.parsing import classify_selection_intent, clarify_destination
from src.bikeshare.station_data import (
    fetch_all_stations,
    fetch_live_status,
    get_station_status,
)
from src.bikeshare.station_search import search_stations
from src.bikeshare.transcription import infer_audio_mime_from_headers, transcribe_audio
from src.bikeshare.trip_state import (
    make_initial_trip_state,
    record_dock_observation,
    record_tick_decision,
)
from src.bikeshare.voice import build_speech_synthesis_html

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(page_title="DockTalk Monitor Debugger", layout="wide")
st.title("🚲 DockTalk Monitor Debugger")

# ── Session state initialisation ───────────────────────────────────────────────
if "trip_state" not in st.session_state:
    st.session_state.trip_state = None
if "tick_history" not in st.session_state:
    st.session_state.tick_history = []
if "last_result" not in st.session_state:
    st.session_state.last_result = None
if "voice_setup" not in st.session_state:
    st.session_state.voice_setup = {
        "phase": "initial",         # initial → confirming → exhausted
        "attempt_history": [],
        "clarification_turns_remaining": 2,
        "candidates": None,
        "last_transcript": None,
        "last_audio_digest": None,
        "pending_speech": None,
        "info_message": None,
    }


def _audio_recorder_widget():
    from audio_recorder_streamlit import audio_recorder

    return audio_recorder


def _audio_digest(audio_bytes: bytes) -> str:
    return hashlib.sha1(audio_bytes).hexdigest()


def _reset_voice_setup() -> None:
    st.session_state.voice_setup = {
        "phase": "initial",
        "attempt_history": [],
        "clarification_turns_remaining": 2,
        "candidates": None,
        "last_transcript": None,
        "last_audio_digest": None,
        "pending_speech": None,
        "info_message": None,
    }


def _speak(message: str) -> None:
    """Queue a spoken message; played on the next rerun once the DOM settles."""
    if not message:
        return
    st.session_state.voice_setup["pending_speech"] = message


def _flush_pending_speech() -> None:
    pending = st.session_state.voice_setup.get("pending_speech")
    if pending:
        components.html(build_speech_synthesis_html(pending), height=0)
        st.session_state.voice_setup["pending_speech"] = None


def _start_monitoring(station_id: str, station_name: str) -> None:
    arrival = datetime.now() + timedelta(minutes=10)
    st.session_state.trip_state = make_initial_trip_state(
        station_id, station_name, arrival
    )
    st.session_state.tick_history = []
    st.session_state.last_result = None
    st.session_state.pop("looked_up", None)
    _reset_voice_setup()
    st.rerun()


# ── 1. Setup Panel ─────────────────────────────────────────────────────────────
monitoring_started = st.session_state.trip_state is not None
with st.expander("⚙️ Setup", expanded=not monitoring_started):
    all_stations = fetch_all_stations()

    openai_key_present = bool(os.getenv("OPENAI_API_KEY"))
    gemini_key_present = bool(os.getenv("GEMINI_API_KEY"))
    voice_enabled = openai_key_present and gemini_key_present

    voice_tab, text_tab = st.tabs(["🎙️ Speak destination", "⌨️ Type destination"])

    # ── Voice tab ────────────────────────────────────────────────────────
    with voice_tab:
        if not voice_enabled:
            missing = []
            if not openai_key_present:
                missing.append("`OPENAI_API_KEY`")
            if not gemini_key_present:
                missing.append("`GEMINI_API_KEY`")
            st.warning(
                "Voice input is unavailable: missing "
                + " and ".join(missing)
                + ". Please use the Text tab."
            )

        voice_state = st.session_state.voice_setup
        audio_recorder = _audio_recorder_widget()

        # The widget MUST render unconditionally on first paint for iOS Safari
        # to fire the mic permission prompt on the tap. We render it whether or
        # not voice is enabled — disabled keys simply produce a stale widget
        # whose bytes we ignore below.
        recorder_disabled = voice_state["phase"] == "exhausted" or not voice_enabled
        audio_bytes = audio_recorder(
            text="Tap to speak",
            recording_color="#de1212",
            neutral_color="#303030",
            icon_size="2x",
            key=f"voice_recorder_{voice_state['phase']}",
        )

        if voice_state["info_message"]:
            st.caption(voice_state["info_message"])

        if voice_state["phase"] == "exhausted":
            st.error(
                "I couldn't pin that down. Please use the Text tab to type your "
                "destination."
            )
            if st.button("Try voice again", key="voice_retry"):
                _reset_voice_setup()
                st.rerun()

        # ── Handle new audio ──────────────────────────────────────────
        if voice_enabled and audio_bytes:
            _log.debug(
                "Received audio_bytes len=%d (digest=%s, recorder_disabled=%s)",
                len(audio_bytes),
                _audio_digest(audio_bytes)[:10],
                recorder_disabled,
            )

        if (
            voice_enabled
            and audio_bytes
            and not recorder_disabled
            and _audio_digest(audio_bytes) != voice_state["last_audio_digest"]
        ):
            voice_state["last_audio_digest"] = _audio_digest(audio_bytes)
            try:
                headers = dict(st.context.headers) if hasattr(st, "context") else {}
                _log.debug("Request User-Agent: %r", headers.get("User-Agent") or headers.get("user-agent"))
                mime = infer_audio_mime_from_headers(headers)
            except Exception as exc:
                _log.warning("Header inspection failed (%s); defaulting to audio/webm", exc)
                mime = "audio/webm"

            _log.info(
                "New recording: %d bytes, mime=%s, phase=%s",
                len(audio_bytes), mime, voice_state["phase"],
            )

            # Optionally dump the raw audio so you can inspect / replay it.
            if VOICE_DEBUG:
                try:
                    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                    ext = "m4a" if mime == "audio/mp4" else "webm"
                    fname = (
                        DEBUG_DIR
                        / f"voice_{int(time.time())}_{_audio_digest(audio_bytes)[:8]}.{ext}"
                    )
                    fname.write_bytes(audio_bytes)
                    _log.info("Wrote debug audio dump to %s", fname)
                except Exception as exc:
                    _log.warning("Could not write debug audio dump: %s", exc)

            try:
                t0 = time.monotonic()
                transcript = transcribe_audio(audio_bytes, mime)
                _log.info(
                    "Whisper returned %.2fs: %r",
                    time.monotonic() - t0, transcript,
                )
            except Exception as exc:
                _log.exception("Whisper call failed: %s", exc)
                transcript = ""
                voice_state["info_message"] = (
                    f"I couldn't hear you, please try again. ({exc.__class__.__name__})"
                )

            if not transcript:
                _log.warning("Empty transcript — Whisper returned no text.")
                voice_state["info_message"] = (
                    "I couldn't hear you, please try again."
                )
            else:
                voice_state["last_transcript"] = transcript

                if voice_state["phase"] == "confirming" and voice_state["candidates"]:
                    intent = classify_selection_intent(
                        transcript, voice_state["candidates"]
                    )
                    _log.info("Confirmation-phase intent: %s", intent)
                    if intent.intent == "select" and intent.index is not None:
                        chosen = voice_state["candidates"][intent.index]
                        _start_monitoring(chosen["station_id"], chosen["name"])
                    elif intent.intent == "new_destination" and intent.transcript:
                        # Voluntary change of mind — re-enter the cascade,
                        # do NOT consume a clarification turn.
                        voice_state["phase"] = "initial"
                        voice_state["candidates"] = None
                        voice_state["info_message"] = None
                        transcript = intent.transcript
                        voice_state["last_transcript"] = transcript
                    else:
                        voice_state["info_message"] = (
                            f"I heard '{transcript}'. Tap one of the cards, "
                            "or try again."
                        )
                        transcript = None  # don't re-run cascade

                if transcript and voice_state["phase"] != "confirming":
                    live_status = fetch_live_status()
                    merged_stations = merge_info_and_status(all_stations, live_status)
                    candidates = resolve_destination(transcript, merged_stations)
                    _log.info(
                        "Cascade returned %d candidate(s): %s",
                        len(candidates),
                        [c.get("name") for c in candidates],
                    )

                    if candidates:
                        voice_state["candidates"] = candidates[:3]
                        voice_state["phase"] = "confirming"
                        voice_state["info_message"] = None
                    else:
                        # Cascade failed → clarification loop
                        voice_state["attempt_history"].append(transcript)
                        if voice_state["clarification_turns_remaining"] > 0:
                            voice_state["clarification_turns_remaining"] -= 1
                            prompt = clarify_destination(
                                voice_state["attempt_history"]
                            )
                            _speak(prompt.spoken_question)
                            voice_state["info_message"] = prompt.spoken_question
                            voice_state["phase"] = "initial"
                            voice_state["candidates"] = None
                        else:
                            voice_state["phase"] = "exhausted"
                            _speak(
                                "I can't pin that down. Please type it instead."
                            )

        # ── Render transcript + candidates ────────────────────────────
        if voice_state["last_transcript"]:
            st.markdown(f"**Heard:** _{voice_state['last_transcript']}_")

        if voice_state["phase"] == "confirming" and voice_state["candidates"]:
            st.markdown("**Pick one — tap a card or speak (e.g. \"first one\"):**")
            cols = st.columns(len(voice_state["candidates"]))
            for idx, (col, candidate) in enumerate(
                zip(cols, voice_state["candidates"])
            ):
                with col:
                    dock_icon = "🟢" if candidate["available_docks"] >= 3 else "🟡"
                    distance = candidate.get("distance_meters", 0)
                    distance_str = (
                        f"~{distance} m away · " if distance else ""
                    )
                    st.markdown(
                        f"**{idx + 1}. {candidate['name']}**  \n"
                        f"{dock_icon} {candidate['available_docks']} docks · "
                        f"{distance_str}"
                        f"_{candidate['recommendation_reason']}_"
                    )
                    if st.button(
                        "Choose",
                        key=f"voice_choose_{idx}",
                        type="primary",
                    ):
                        _start_monitoring(
                            candidate["station_id"], candidate["name"]
                        )

        _flush_pending_speech()

        st.caption(
            "Geocoding via Nominatim — © OpenStreetMap contributors."
        )

    # ── Text tab ─────────────────────────────────────────────────────────
    with text_tab:
        query = st.text_input(
            "Search station by name",
            key="station_query",
            placeholder="e.g. Union Station, Bay St, King",
        )

        selected_id: str | None = None
        selected_name: str | None = None

        if query.strip():
            matches = search_stations(query.strip(), all_stations)
            if matches:
                option_labels = [
                    f"{m['name']}  ({m['station_id']})" for m in matches
                ]
                chosen_label = st.selectbox(
                    "Matching stations", option_labels, key="station_select"
                )
                chosen_idx = option_labels.index(chosen_label)
                selected_id = matches[chosen_idx]["station_id"]
                selected_name = matches[chosen_idx]["name"]
            else:
                st.caption("No stations match that name.")

        col_lu, col_start = st.columns([1, 1])

        with col_lu:
            if st.button(
                "🔍 Look Up", disabled=selected_id is None, key="btn_lookup"
            ):
                st.session_state.looked_up = get_station_status(selected_id)

            if "looked_up" in st.session_state:
                lu = st.session_state.looked_up
                dock_icon = "🟢" if lu["num_docks_available"] >= 3 else "🔴"
                st.info(
                    f"**{lu['name']}**  \n"
                    f"{dock_icon} {lu['num_docks_available']} docks available · "
                    f"status: `{lu['station_status']}`"
                )

        with col_start:
            if st.button(
                "▶ Start Monitoring",
                disabled=selected_id is None,
                key="btn_start",
                type="primary",
            ):
                _start_monitoring(selected_id, selected_name)


# ── Guard: nothing below until monitoring starts ────────────────────────────────
if st.session_state.trip_state is None:
    st.stop()

trip_state: dict = st.session_state.trip_state
is_finished = trip_state.get("status") == "finished"

st.divider()


# ── 2. ETA Controls ────────────────────────────────────────────────────────────
st.subheader("ETA")

eta_c1, eta_c2, eta_c3, eta_c4 = st.columns(4)
with eta_c1:
    if st.button("10 min away", disabled=is_finished):
        trip_state["arrival_time"] = datetime.now() + timedelta(minutes=10)
        st.rerun()
with eta_c2:
    if st.button("5 min away", disabled=is_finished):
        trip_state["arrival_time"] = datetime.now() + timedelta(minutes=5)
        st.rerun()
with eta_c3:
    if st.button("2 min away", disabled=is_finished):
        trip_state["arrival_time"] = datetime.now() + timedelta(minutes=2)
        st.rerun()
with eta_c4:
    if st.button("Arrived", disabled=is_finished):
        trip_state["status"] = "finished"
        trip_state["finish_reason"] = "rider pressed Arrived"
        st.rerun()

now = datetime.now()
arrival = trip_state["arrival_time"]
eta_seconds = max(0, int((arrival - now).total_seconds()))
eta_min, eta_sec = divmod(eta_seconds, 60)
st.caption(f"ETA: **{eta_min}m {eta_sec:02d}s**  ·  arrives at {arrival.strftime('%H:%M:%S')}")


# ── 3. Run Tick + Status Bar ───────────────────────────────────────────────────
st.divider()

if st.button(
    "▶ Run Monitor Tick",
    type="primary",
    disabled=is_finished,
    key="btn_tick",
):
    live = get_station_status(trip_state["target_station_id"])
    record_dock_observation(
        trip_state,
        docks_available=live["num_docks_available"],
        observed_at=datetime.now(timezone.utc).isoformat(),
    )
    result = run_tick(trip_state)
    record_tick_decision(trip_state)
    trip_state["target_just_switched"] = False
    st.session_state.last_result = result
    st.session_state.tick_history.append((result, copy.deepcopy(trip_state)))
    st.rerun()

# Status bar
dock_history = trip_state.get("dock_history", [])
live_docks_str = str(dock_history[-1]["docks_available"]) if dock_history else "—"

status_icon = {
    "monitoring": "🟢",
    "alerted": "🔴",
    "finished": "⬛",
}.get(trip_state.get("status", ""), "⚪")

source_str = ""
if st.session_state.last_result:
    src = st.session_state.last_result.get("source", "")
    source_str = "  ·  🤖 LLM" if src == "llm" else "  ·  ⚙️ fallback"

next_check = trip_state.get("next_check_seconds")
next_check_str = f"{next_check}s" if next_check is not None else "—"

tick_count = len(st.session_state.tick_history)

st.markdown(
    f"**{trip_state['target_station_name']}**"
    f"  ·  🅿️ {live_docks_str} docks"
    f"  ·  ⏱ {eta_min}m {eta_sec:02d}s"
    f"  ·  {status_icon} {trip_state.get('status', '—')}"
    f"  ·  next check: {next_check_str}"
    f"  ·  ticks: {tick_count}"
    f"{source_str}"
)


# ── 4. Alert Panel ─────────────────────────────────────────────────────────────
if trip_state.get("status") == "alerted" and trip_state.get("alert"):
    alert = trip_state["alert"]
    st.divider()
    st.error(f"🚨 **{alert.get('headline', 'Alert')}**")
    st.write(alert.get("message", ""))

    alternatives = alert.get("alternatives", [])
    if alternatives:
        cols = st.columns(min(len(alternatives), 3))
        for i, alt in enumerate(alternatives[:3]):
            with cols[i]:
                name = alt.get("station_name") or alt.get("station_id", "Unknown")
                st.markdown(f"**{name}**")
                st.metric("Docks available", alt.get("docks_available", "?"))
                if alt.get("reason"):
                    st.caption(alt["reason"])
                if st.button("Switch here", key=f"switch_{alt['station_id']}"):
                    old_id = trip_state["target_station_id"]
                    trip_state["rejected_station_ids"].append(old_id)
                    trip_state["target_station_id"] = alt["station_id"]
                    trip_state["target_station_name"] = name
                    trip_state["target_just_switched"] = True
                    trip_state["status"] = "monitoring"
                    del trip_state["alert"]
                    st.rerun()


# ── 5. Trace Expander ──────────────────────────────────────────────────────────
if st.session_state.last_result:
    result = st.session_state.last_result
    trace = result.get("trace", [])
    fallback_reason = result.get("fallback_reason")

    with st.expander(f"Tick trace  ({len(trace)} steps)", expanded=True):
        if fallback_reason:
            st.warning(f"**Fallback triggered:** {fallback_reason}")

        if not trace:
            st.caption("No trace steps recorded.")

        for step in trace:
            step_type = step.get("type", "")

            if step_type == "thinking":
                st.markdown(
                    f"<span style='color:grey;font-style:italic'>{step.get('text', '')}</span>",
                    unsafe_allow_html=True,
                )

            elif step_type == "tool_call":
                import json
                st.markdown(f"**🔧 `{step.get('tool', '')}`**")
                st.code(
                    f"args:\n{json.dumps(step.get('args', {}), indent=2, default=str)}\n\n"
                    f"result:\n{json.dumps(step.get('result', {}), indent=2, default=str)}",
                    language="json",
                )

            elif step_type == "fallback":
                st.warning(
                    f"**Fallback** — {step.get('reason', '')}  \n"
                    f"docks={step.get('observed_docks', '?')}  ·  "
                    f"ETA={step.get('minutes_to_arrival', '?')} min  ·  "
                    f"status={step.get('station_status', '?')}"
                )

            elif step_type == "llm_error":
                st.error(
                    f"**LLM error — {step.get('error_type', 'unknown')}**  \n"
                    f"{step.get('message', '')}"
                )

            elif step_type == "tool_limit":
                st.warning(step.get("message", "Tool call limit reached."))
