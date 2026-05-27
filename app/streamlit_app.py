"""DockTalk Monitor Debugger — Streamlit app.

Single-page debugger for the DockTalk monitor loop. Pick a destination
station, set an ETA, and fire ticks one at a time to watch the agent
observe dock counts, call tools, and decide what to do next.
"""

import copy
import sys
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

from src.bikeshare.agent import run_tick
from src.bikeshare.station_data import fetch_all_stations, get_station_status
from src.bikeshare.station_search import search_stations
from src.bikeshare.trip_state import (
    make_initial_trip_state,
    record_dock_observation,
    record_tick_decision,
)

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


# ── 1. Setup Panel ─────────────────────────────────────────────────────────────
monitoring_started = st.session_state.trip_state is not None
with st.expander("⚙️ Setup", expanded=not monitoring_started):
    all_stations = fetch_all_stations()

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
            option_labels = [f"{m['name']}  ({m['station_id']})" for m in matches]
            chosen_label = st.selectbox("Matching stations", option_labels, key="station_select")
            chosen_idx = option_labels.index(chosen_label)
            selected_id = matches[chosen_idx]["station_id"]
            selected_name = matches[chosen_idx]["name"]
        else:
            st.caption("No stations match that name.")

    col_lu, col_start = st.columns([1, 1])

    with col_lu:
        if st.button("🔍 Look Up", disabled=selected_id is None, key="btn_lookup"):
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
            arrival = datetime.now() + timedelta(minutes=10)
            st.session_state.trip_state = make_initial_trip_state(
                selected_id, selected_name, arrival
            )
            st.session_state.tick_history = []
            st.session_state.last_result = None
            st.session_state.pop("looked_up", None)
            st.rerun()


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
