# Converting DockTalk from Streamlit to FastAPI + Gemini Live API

Status: design draft — open questions resolved (see `docs/adr/`)
Date: 2026-05-27

## Why Convert

The current Streamlit app was designed as a monitor debugger, not a production voice assistant. Its core constraint is the **full-page rerun model**: every user interaction re-executes the entire Python script. This makes real-time, continuous voice conversation impossible.

Specific problems that motivate the change:

| Problem | Root cause |
|---|---|
| High latency between rider speech and system response | Batch pipeline: record full clip → upload → Whisper → Gemini → inject `<script>` TTS → next rerun |
| Cannot listen while speaking | Audio recorder and speech synthesis can't run concurrently inside Streamlit's rerun cycle |
| Monitor loop requires manual intervention | The debugger has a "Run Monitor Tick" button; a real app needs a background poll loop independent of UI events |
| Rider commands during monitoring are unimplemented | The command loop spec (`docs/rider_journey.md`) cannot be wired to the Streamlit rerun model without brittle workarounds |
| Session state is fragile | `st.session_state` is local to a single browser session and is lost on page reload; there is no persistence path |
| iOS / browser compatibility held together by workarounds | The audio-recorder-streamlit widget, MIME inference from User-Agent, `SpeechSynthesisUtterance` injection via `components.html` — each is a workaround for a Streamlit constraint |

**What the Gemini Live API solves:**

Gemini Live API is a single persistent WebSocket that streams audio in both directions, performs continuous speech recognition, supports function calling mid-conversation, and generates spoken responses — all in one connection. This replaces the three-stage Whisper → Gemini → TTS pipeline with a live conversation loop that has sub-second response latency.

Gemini Live is chosen over OpenAI's Realtime API because the monitor loop already uses Gemini Flash for risk evaluation and spoken alert wording. Keeping both the conversation loop and the monitor loop on the same model family avoids re-solving the "do not invent station facts" constraint in a second model and removes the cross-provider seam where Gemini-generated alert wording would be spoken by an OpenAI voice. See `docs/adr/0001-gemini-live-as-conversation-layer.md`.

**What FastAPI solves:**

FastAPI provides an async Python server that can:
- Maintain a WebSocket connection to the browser
- Bridge that connection to the Gemini Live API WebSocket
- Run the monitor poll loop as an independent asyncio background task
- Expose REST endpoints for station data and session management

---

## What Changes

### Frontend

| Current (Streamlit) | New (HTML + JS) |
|---|---|
| Streamlit page with tabs, expanders, and buttons | Single-page web app served by FastAPI (`/`) |
| `audio-recorder-streamlit` widget | Browser `RTCPeerConnection` or raw `MediaStream` feeding a WebSocket |
| `SpeechSynthesisUtterance` injected via `components.html` | Audio received from Gemini Live API played via `<audio>` element |
| Cards and buttons for confirmation and commands | Minimal voice-first UI; tap targets kept for confirmation only |

The frontend is a thin relay with no logic or framework: open a WebSocket to the FastAPI server, stream microphone audio, and play back received audio. Visual status (target station, dock count, ETA, risk state) is pushed from the server via the same WebSocket as JSON events alongside the audio. See `docs/adr/0004-thin-frontend-no-js-framework.md`.

### Backend

| Current (Streamlit) | New (FastAPI) |
|---|---|
| Streamlit script re-runs on every event | FastAPI async request handlers; state lives in server memory |
| `st.session_state` for monitor and voice state | Per-session state dict, keyed by session ID, held in FastAPI process memory |
| Monitor tick fired manually (debug button) | `asyncio` background task, polling every 60 seconds per the monitoring spec |
| Whisper for transcription | Replaced by Gemini Live API (audio input streamed directly) |
| Browser `SpeechSynthesisUtterance` for TTS | Replaced by Gemini Live API (audio output streamed back) |

### Voice pipeline

```
Before:
  Rider speaks → audio-recorder-streamlit captures chunk → Python receives bytes
  → transcribe_audio() [Whisper HTTP] → transcript string
  → resolve_destination() / classify_selection_intent() [Gemini HTTP]
  → build_speech_synthesis_html() → components.html() → browser TTS

After:
  Rider speaks → browser streams PCM audio via WebSocket →
  FastAPI WebSocket handler relays to Gemini Live API WebSocket →
  Gemini Live performs continuous STT + function calling + TTS →
  FastAPI relays audio back to browser → browser plays audio element
  (simultaneously: Live function calls execute synchronously in Python)
```

### What stays the same

The following Python modules are **unchanged** in responsibility and interface:

- `src/bikeshare/station_data.py` — GBFS feed fetching
- `src/bikeshare/station_search.py` — name index search
- `src/bikeshare/ranking.py` — distance and dock scoring
- `src/bikeshare/destination_resolver.py` — resolution cascade (Step 1 name match, Step 2 geocoding)
- `src/bikeshare/geocoding.py` — Nominatim fallback
- `src/bikeshare/trip_state.py` — trip state shape and mutation helpers
- `src/bikeshare/agent.py` (`run_tick`) — monitor tick logic and Gemini risk evaluation
- All of `docktalk/agent/` — Gemini tool calling for risk evaluation

Gemini is **not replaced** by Gemini Live. The risk evaluator (Gemini Flash, called by `run_tick()`) continues to own:
- Risk evaluation with live dock data (the conversation layer has no access to external station data)
- Generating spoken alert wording
- Station recommendation reasoning

Gemini Live owns the **conversation loop**: understanding what the rider said, routing to the correct tool, and producing the spoken response. It calls Python-defined tools; Python executes them and returns structured results; Gemini Live speaks the outcome.

Monitor loop alerts (from the risk evaluator) are injected into the active Gemini Live session as client-content turns. The system prompt instructs the model to speak the injected `spoken_message` verbatim, so the risk evaluator's grounded wording reaches the rider without rephrasing. See `docs/adr/0002-monitor-alerts-injected-into-live-session.md`.

---

## New Architecture

```
Browser
  │
  │  WebSocket (audio PCM + JSON events, bidirectional)
  │
FastAPI server
  ├── WebSocket handler  ──────────────────────────────────────────────►  Gemini Live API
  │     • relays browser audio upstream                                     WebSocket
  │     • relays Live audio/events downstream                               • continuous STT
  │     • dispatches function calls to Python tool handlers                 • function calling
  │     • pushes status JSON to browser                                     • TTS audio output
  │
  ├── Monitor background task (asyncio, per session)
  │     • polls GBFS every 60 s
  │     • calls run_tick() → Gemini Flash for risk evaluation
  │     • injects spoken_message into Live session as verbatim-speak turn
  │     • survives WebSocket disconnects; delivers snapshot on reconnect
  │
  ├── REST endpoints
  │     GET  /session              — start a new session, return session_id
  │     POST /session/{id}/stop    — rider returned the bike / cancel
  │     GET  /stations             — station metadata (cached)
  │
  └── Session state store (in-process dict, session_id → MonitorState)
```

---

## Gemini Live API Integration

### Connection model

The FastAPI server — not the browser — opens the WebSocket connection to the Gemini Live API (`wss://generativelanguage.googleapis.com/...`). This keeps the `GOOGLE_API_KEY` server-side and avoids exposing it to the browser. The browser connects only to the FastAPI WebSocket at `ws://localhost/ws/{session_id}`.

FastAPI acts as a transparent proxy for audio frames and as an active participant for tool-call events (intercept → execute Python → return result). It also injects monitor-loop alerts into the live session as client-content turns.

**Model**: `gemini-3.1-flash-live-preview` (via `v1alpha` API version). Requires a paid API key; Gemini Live is not available on free-tier keys.

**Push-to-talk VAD**: Automatic voice activity detection is disabled in the `LiveConnectConfig`. The browser sends explicit `start_of_speech` / `end_of_speech` JSON control frames when the rider presses and releases the hold-to-talk button. The server forwards these as `ActivityStart` / `ActivityEnd` realtime input signals. This eliminates the 1–3 s silence-detection delay that VAD would otherwise impose between button release and model response.

### Function tools exposed to Gemini Live

The Live session is configured with a set of `FunctionDeclaration` tools Python will execute. These reuse the same schema shape as the existing `docktalk/agent/` Gemini tools:

| Tool | Python handler | Purpose |
|---|---|---|
| `resolve_destination` | `destination_resolver.resolve_destination()` | Destination entry: turn rider utterance into ranked candidate stations |
| `confirm_station` | `trip_state.make_initial_trip_state()` | Rider picks a candidate; start monitoring |
| `get_station_status` | `station_data.get_station_status()` | Real-time dock count for the target station |
| `get_backup_options` | `ranking.get_best_backups()` | Rider asks "what are my options?" |
| `switch_station` | `trip_state.switch_target()` | Rider confirms a switch recommendation |
| `stop_monitoring` | `trip_state.stop()` | Rider returned the bike or cancelled |
| `get_risk_summary` | `agent.run_tick()` + Gemini Flash | Rider asks "any update?" |

**Ownership rule (unchanged):** Python fetches the data, computes distances, scores stations, and filters offline/zero-dock candidates before passing a bounded candidate list to the conversation layer. The conversation layer's spoken responses are grounded in that data; it cannot invent station names, dock counts, or intersections.

### System prompt

A Gemini Live system prompt is derived from `docktalk/agent/prompts/system.md`. The prompt constrains the model to:
- Act as DockTalk (voice assistant for Bike Share Toronto)
- Only speak station facts that come from tool call results
- Speak injected monitor alert messages verbatim, without rephrasing
- Follow the spoken recommendation formats from `docs/station_recommendation_contract.md`
- Recognise the five v1 rider commands and route them to the correct tools
- Respond briefly; the rider is on a bike

---

## State Model

The state model from `docs/rider_journey.md` is **unchanged**:

```
NOT_STARTED → AWAITING_CONFIRMATION → MONITORING_SAFE ↔ MONITORING_WATCH
  ↔ MONITORING_WARNING → SWITCH_RECOMMENDED → STOPPED
```

State is now held per-session in the FastAPI process (a dict keyed by session ID) rather than in `st.session_state`. The `MonitorState` shape from `docs/monitoring_spec.md` is carried over unchanged.

---

## Monitor Loop

The monitor poll loop moves from a Streamlit button to an `asyncio` background task created when `confirm_station` is called. The task survives WebSocket disconnects; on reconnect, the handler calls `run_tick()` once to deliver a current-state snapshot to the rider. See `docs/adr/0003-monitor-survives-disconnect-snapshot-on-reconnect.md`.

```python
async def monitor_loop(session_id: str):
    while state.status not in ("STOPPED", "FINISHED"):
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        live = fetch_live_status()
        result = run_tick(state)
        if result.should_speak and websocket_connected(session_id):
            await inject_spoken_alert(session_id, result.spoken_message)
```

Session cleanup (task cancellation, state removal) is triggered by `POST /session/{id}/stop` or by the `max_stale_minutes=10` timeout — not by WebSocket close.

All timing defaults from `docs/monitoring_spec.md` are preserved:
`poll_interval_seconds=60`, `quick_retry_seconds=15`, `max_quick_retries=3`, `warning_cooldown_seconds=180`, `max_stale_minutes=10`.

---

## What Happens to the Streamlit App

The Streamlit app (`app/streamlit_app.py`) becomes a **development-only tool** for debugging the monitor tick and risk evaluation logic in isolation. It is not removed, but it is not the production interface.

Production entry point becomes:

```
uvicorn app.server:app --host 0.0.0.0 --port 8000
```

---

## New Files

| File | Purpose |
|---|---|
| `app/server.py` | FastAPI application: WebSocket handler, REST endpoints, session state store |
| `app/live_bridge.py` | Proxies audio and events between browser WebSocket and Gemini Live WebSocket; dispatches tool calls; injects monitor alerts as verbatim-speak turns |
| `app/monitor_task.py` | `asyncio` monitor loop; survives WebSocket disconnects; wraps `run_tick()` and injects alerts into the Live session |
| `app/tools.py` | Maps Gemini Live tool names to Python handler functions |
| `app/static/index.html` | Single-page frontend: microphone capture, WebSocket client, audio playback, minimal status display |
| `app/static/client.js` | WebSocket + MediaStream logic (no framework) |
| `docktalk/agent/prompts/live_system.md` | Gemini Live system prompt derived from `system.md`; adds verbatim-alert instruction |

---

## Dependencies

Add to `pyproject.toml`:

```toml
"fastapi>=0.111",
"uvicorn[standard]>=0.29",
"websockets>=12",
```

Remove (no longer needed in production path, keep for Streamlit debugger):

```toml
"streamlit>=1.30",          # move to dev / optional dependency
"audio-recorder-streamlit", # Streamlit-only
```

`openai>=1.0` — keep only if Whisper is used in the Streamlit debugger; otherwise remove from production dependencies.

---

## What Is Not Changing in This Conversion

- The GBFS data pipeline — no change
- Station ranking, scoring, and backup selection logic — no change
- Gemini risk evaluation and alert wording generation — no change
- The `MonitorState` shape and state machine — no change
- The station recommendation contract — no change
- The spoken recommendation formats — no change
- Tests for `transcription.py`, `parsing.py`, `geocoding.py`, `destination_resolver.py` — no change

---

## Decisions

All four open questions have been resolved. See `docs/adr/` for the rationale:

1. **Session persistence across disconnects** → monitor task survives; reconnect delivers a current-state snapshot. (`docs/adr/0003-monitor-survives-disconnect-snapshot-on-reconnect.md`)

2. **Conversation-layer provider** → Gemini Live API, not OpenAI Realtime API. (`docs/adr/0001-gemini-live-as-conversation-layer.md`)

3. **Frontend complexity** → thin relay, no JS framework. (`docs/adr/0004-thin-frontend-no-js-framework.md`)

4. **Deployment target** → local-only for v1 (`uvicorn` on localhost). Cloud deployment (Fly.io or Railway) deferred until a production runtime is needed; no ADR until that decision is made.
