# PRD: Convert DockTalk from Streamlit to FastAPI + Gemini Live API

**Status:** ready-for-agent
**Date:** 2026-05-27

---

## Problem Statement

DockTalk was prototyped in Streamlit, which re-executes the entire Python script on every UI event. This full-page rerun model makes continuous voice conversation impossible: the app cannot listen and speak at the same time, the monitor loop requires a manual button press instead of running in the background, rider commands during monitoring are unimplemented, and the session is lost on any page reload. The current implementation is a monitor debugger, not a usable voice assistant.

---

## Solution

Replace the Streamlit frontend and batch voice pipeline with a FastAPI server that bridges the browser to the Gemini Live API over a persistent WebSocket. The rider speaks; Gemini Live transcribes, routes to tools, and speaks back — all in one live connection. A separate asyncio background task runs the monitor loop, polling GBFS data and injecting spoken alerts into the live session when dock availability changes. The existing Python data and reasoning modules are untouched.

---

## User Stories

1. As a rider, I want to speak my destination and hear a station recommendation within one second, so that I can keep my eyes on the road and not wait for a slow batch pipeline.
2. As a rider, I want the app to listen for my response while it is speaking to me, so that I can interrupt or confirm without waiting for it to finish.
3. As a rider, I want the monitor to run in the background without any button presses, so that I receive alerts automatically while I ride.
4. As a rider, I want to hear an alert when my target station is filling up, so that I can decide to switch before I arrive.
5. As a rider, I want to hear an alert when my target station goes offline, so that I am not stranded looking for a dock on foot.
6. As a rider, I want to say "what are my options?" and hear up to three nearby backup stations with dock counts, so that I can make an informed switch decision.
7. As a rider, I want to say "switch to [station name]" and have monitoring transfer to that station immediately, so that I do not have to restart the app.
8. As a rider, I want to say "any update?" and hear a current summary of dock availability, so that I can check in on demand without waiting for the next automatic alert.
9. As a rider, I want to say "cancel" or "I returned the bike" and have monitoring stop, so that the app does not continue alerting me after my trip ends.
10. As a rider, I want the monitor to keep running if I lose signal briefly, so that I do not miss a critical alert because of a 15-second signal drop.
11. As a rider, I want to hear a current-state summary when I reconnect after a signal drop, so that I know immediately whether my target station is still viable.
12. As a rider, I want the app to work on my phone browser without installing anything, so that I can use it on any iOS or Android device.
13. As a rider, I want spoken responses to be brief, so that I can absorb the information while cycling without being distracted.
14. As a rider, I want the app to only speak station names and dock counts that are real, so that I am never directed to a station that does not exist.
15. As a rider, I want to say a destination in natural language (an intersection, a landmark, a neighbourhood) and have it resolve to real stations, so that I do not need to know station IDs.
16. As a rider, I want the app to offer me a visual tap target for confirming my station selection, so that I can confirm without speaking in a noisy environment.
17. As a rider, I want to see the current target station name, dock count, and risk state on screen, so that I can glance at the display for a quick check.
18. As a developer, I want the Streamlit app to remain available as a debug tool for the monitor tick logic, so that I can test risk evaluation scenarios without needing a live browser WebSocket session.
19. As a developer, I want the conversation-layer tools to share the same `FunctionDeclaration` schema shape as the existing Gemini risk-evaluator tools, so that there is one tool definition pattern to maintain.
20. As a developer, I want the monitor background task to self-terminate after 10 minutes without fresh GBFS data, so that stale sessions do not accumulate in process memory.

---

## Implementation Decisions

### New modules to build

**`app/server.py` — FastAPI application**
Central entry point. Owns:
- `GET /` — serves `app/static/index.html`
- `WebSocket /ws/{session_id}` — accepts browser connection; delegates audio relay to `live_bridge`; receives status events from `monitor_task` and forwards them to the browser as JSON
- `GET /session` — creates a new session ID, initialises a `TripState` entry in the in-process session store, returns `session_id`
- `POST /session/{id}/stop` — sets `TripState.status = STOPPED`, cancels the asyncio monitor task
- `GET /stations` — returns cached station metadata (calls `fetch_all_stations()`)
- In-process session store: a plain `dict[str, SessionRecord]` where `SessionRecord` holds the `TripState` and a reference to the active WebSocket and asyncio task

**`app/live_bridge.py` — Gemini Live proxy**
Opens the Gemini Live WebSocket server-side when a browser session connects. Runs two concurrent coroutines:
- *upstream*: reads PCM audio chunks from the browser WebSocket and writes them to the Live WebSocket as `realtime_input` messages
- *downstream*: reads events from the Live WebSocket; relays audio frames to the browser WebSocket; intercepts `tool_call` events and dispatches them to `app/tools.py`; returns `tool_response` parts back to Live

Monitor alerts arrive as a queue item from `monitor_task`; `live_bridge` injects them as `client_content` turns with a `verbatim_speak` instruction.

**`app/monitor_task.py` — asyncio background monitor**
Created as an asyncio task when `confirm_station` tool is called. Runs a loop:
1. `asyncio.sleep(poll_interval_seconds)` (default 60 s)
2. `fetch_live_status()` — GBFS poll
3. `record_dock_observation()` — append to `TripState.dock_history`
4. `run_tick(trip_state)` — Gemini Flash risk evaluation
5. `record_tick_decision()` — update `TripState.recent_decisions`
6. If `result.should_speak` and a WebSocket is connected: enqueue `spoken_message` for `live_bridge` to inject
7. If WebSocket is not connected: discard the alert (reconnect delivers a snapshot instead)

On reconnect: calls `run_tick()` once immediately and enqueues the result as a snapshot alert regardless of cooldown.

Task terminates when `TripState.status` is `STOPPED` or when `fetch_failure_count` reaches the `max_stale_minutes=10` threshold.

**`app/tools.py` — conversation-loop tool registry**
Maps Gemini Live tool names to Python handlers. Shares `FunctionDeclaration` schema shape with existing `src/bikeshare/tools.py`. Tools:

| Tool | Handler |
|---|---|
| `resolve_destination` | `destination_resolver.resolve_destination()` + `merge_info_and_status()` |
| `confirm_station` | `trip_state.make_initial_trip_state()` + spawn monitor task |
| `get_station_status` | `station_data.get_station_status()` |
| `get_backup_options` | `station_data.get_nearby_stations()` filtered to active, ≥1 dock, ≤800 m, excluding target |
| `switch_station` | update `TripState.target_station_id`, call `record_tick_decision()` |
| `stop_monitoring` | set `TripState.status = STOPPED`, cancel monitor task |
| `get_risk_summary` | call `run_tick(trip_state)` + return `spoken_message` for Live to speak |

**`app/static/index.html` + `app/static/client.js` — thin browser client**
No JS framework. `client.js` does exactly three things:
1. Opens `MediaStream` from `getUserMedia({ audio: true })` and streams PCM audio upstream via WebSocket
2. Receives audio frames from the WebSocket and plays them via a single `<audio>` element
3. Receives JSON status events and updates a small set of DOM elements: target station name, dock count, risk state

One tap target: a "Confirm" button shown during `AWAITING_CONFIRMATION` state, hidden otherwise.

**`docktalk/agent/prompts/live_system.md` — Gemini Live system prompt**
Derived from `prompt/04_monitor_agent.txt` and the existing `docktalk/agent/prompts/`. Constraints:
- Only speak station names, dock counts, and intersections from tool call results
- When a `VERBATIM_ALERT:` prefix is present in a client-content turn, speak the content exactly as written, no rephrasing
- Recognise the five v1 rider commands and route to correct tools
- Respond in ≤2 sentences; the rider is cycling

### Modules unchanged in interface

- `src/bikeshare/station_data.py` — GBFS fetching and caching
- `src/bikeshare/station_search.py` — name-index search
- `src/bikeshare/destination_resolver.py` — resolution cascade
- `src/bikeshare/geocoding.py` — Nominatim fallback
- `src/bikeshare/trip_state.py` — TripState shape and mutation helpers
- `src/bikeshare/agent.py` (`run_tick`) — Gemini Flash risk evaluation tick
- `src/bikeshare/tools.py` — monitor-agent tool dispatch (used by `run_tick`)

### Dependency changes (`pyproject.toml`)

Add to production dependencies:
```
fastapi>=0.111
uvicorn[standard]>=0.29
websockets>=12
```

Move to `[project.optional-dependencies].dev`:
```
streamlit>=1.30
audio-recorder-streamlit
```

`openai>=1.0` — retain only if Whisper is used in the Streamlit debugger; otherwise remove from production.

### Session lifecycle

```
GET /session          → session_id created, TripState = NOT_STARTED
WebSocket /ws/{id}    → live_bridge opens Gemini Live session
resolve_destination   → TripState = AWAITING_CONFIRMATION
confirm_station       → TripState = MONITORING_SAFE, monitor_task spawned
[monitor loop runs]   → TripState transitions: SAFE ↔ WATCH ↔ WARNING → SWITCH_RECOMMENDED
stop_monitoring       → TripState = STOPPED, monitor_task cancelled, session GC'd
```

### Monitor alert injection protocol

When `monitor_task` has a `spoken_message` to deliver, it enqueues:
```
VERBATIM_ALERT: <spoken_message text>
```
`live_bridge` injects this as a `client_content` turn. The system prompt instructs the model to speak it exactly. This preserves the risk evaluator's grounded wording.

---

## Testing Decisions

**What makes a good test:** test observable outputs given controlled inputs; do not assert on internal state mutation order or intermediate data shapes. Inject dependencies (GBFS fetch, Gemini calls) via the existing callable-override seams already present in `destination_resolver.py` and `agent.py`.

**Modules to test:**

- **`app/tools.py`** — unit test each tool handler with mock station data. Assert that `resolve_destination` returns recommendation objects with all required fields; that `confirm_station` creates a valid `TripState`; that `get_backup_options` excludes the target station, offline stations, and zero-dock stations; that `stop_monitoring` sets status to `STOPPED`. Prior art: `tests/test_destination_resolver.py` uses callable overrides to avoid live GBFS/Gemini calls — same pattern applies.

- **`app/monitor_task.py`** — unit test the tick loop logic in isolation: given a `TripState` and a mocked `run_tick()` return value, assert the correct alert is enqueued when `should_speak=True`, nothing is enqueued when `should_speak=False`, and the task terminates when `status=STOPPED`. Do not test asyncio scheduling directly.

- **`src/bikeshare/trip_state.py`** — already has `tests/test_trip_state.py`; extend to cover `switch_target` mutation if added.

- **`src/bikeshare/destination_resolver.py`**, **`src/bikeshare/geocoding.py`**, **`src/bikeshare/station_search.py`** — existing test suite unchanged.

**Do not test:**
- `app/live_bridge.py` — the Gemini Live WebSocket proxy. The interesting behaviour here is the network relay, which requires a live connection. Favour integration testing at the `tools.py` boundary instead.
- `app/server.py` routing — FastAPI endpoint wiring is shallow; test the handlers it delegates to instead.

---

## Out of Scope

- **iOS native app** — the frontend targets mobile browser, not a native app.
- **Authentication / multi-user sessions** — v1 is single-user, local-only. Session IDs are not authenticated.
- **Cloud deployment** — production deployment to Fly.io, Railway, or any cloud runtime is deferred. v1 entry point is `uvicorn app.server:app` on localhost.
- **ETA input** — riders do not input an estimated arrival time in v1; the monitor uses a fixed polling interval.
- **Push notifications** — alerts are delivered only while the browser WebSocket is connected.
- **Persistent trip history** — no database; session state lives in process memory only.
- **Rider preferences** — the `preferences` field in `TripState` is populated as an empty list in v1.

---

## Further Notes

- The Streamlit app (`app/streamlit_app.py`) is retained as a development-only tool for debugging monitor tick and risk evaluation logic in isolation. It is not the production interface and should be moved to `[project.optional-dependencies].dev` or documented as dev-only.
- The `station_name` field in backup recommendation objects must use the location-hint rules from `docs/station_recommendation_contract.md` — official name if it contains an intersection, official address, curated alias, or honest weaker phrase. The conversation-layer model must not invent intersections.
- Gemini Live API is newer than OpenAI Realtime; function-calling ergonomics should be validated against the current SDK (`google-genai`) before relying on any specific event shape. See `docs/adr/0001-gemini-live-as-conversation-layer.md` for the rationale and reversal cost.
