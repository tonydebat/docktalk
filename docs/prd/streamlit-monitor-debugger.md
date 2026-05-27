# PRD: Streamlit Monitor Debugger

**Label:** ready-for-agent

---

## Problem Statement

The DockTalk monitor agent (`run_tick()`) is working end-to-end, but there is no way to exercise it interactively without writing code. A developer or product owner who wants to observe how the agent behaves — which tools it calls, what decisions it makes, when it fires an alert — must either run a script and read JSON traces in a terminal, or instrument code manually. There is no way to choose a real Toronto station, watch dock counts evolve tick by tick, and inspect the agent's reasoning in a readable format.

---

## Solution

A single-page Streamlit app that acts as a manual debugger for the monitor loop. The developer picks a destination station by name, sets an ETA, and fires ticks one at a time. After each tick the app shows what the agent observed, what tools it called, what decision it reached, and — if the agent fired an alert — the recommended alternative stations. Each panel in the app maps directly to a concept in the monitor loop, making it easy to understand what the agent is doing and why.

---

## User Stories

1. As a developer, I want to search for a station by name so that I do not need to look up raw station IDs manually.
2. As a developer, I want to see a filtered list of matching stations as I type so that I can pick the right one quickly.
3. As a developer, I want to confirm my station selection and see its current live dock count before starting so that I know the station is live and accepting returns.
4. As a developer, I want to click a single Start Monitoring button to initialise the trip so that I can begin a fresh session without reloading the page.
5. As a developer, I want the trip state to reset cleanly when I start a new monitoring session so that dock history and past decisions from a previous run do not contaminate the new one.
6. As a developer, I want to set my ETA to 10, 5, or 2 minutes from now with a single button press so that I can quickly simulate different urgency scenarios.
7. As a developer, I want to mark the trip as arrived with a single button press so that I can simulate trip completion without running a tick.
8. As a developer, I want to see the current ETA countdown on every render so that I know how much time the agent believes remains.
9. As a developer, I want to fire a single monitor tick by pressing a button so that I have full control over the pace of the simulation.
10. As a developer, I want a live dock count observation to be recorded in the dock history automatically before each tick so that the agent always has fresh, timestamped data to reason over.
11. As a developer, I want a compact status bar after each tick showing the target station, live dock count, ETA, monitor status, next check interval, and whether the tick was served by the LLM or the fallback so that I can assess agent health at a glance.
12. As a developer, I want to see an alert panel whenever the agent calls `alert_user` so that I can read the headline, message, and recommended alternative stations.
13. As a developer, I want each alternative station in the alert panel to show as a card with a Switch button so that I can simulate the rider choosing a different destination.
14. As a developer, I want switching to an alternative station to update the target, set the `target_just_switched` flag, add the old target to `rejected_station_ids`, and clear the active alert so that the agent correctly understands the context on the next tick.
15. As a developer, I want the alert panel to disappear after I switch stations so that the UI accurately reflects the current monitoring state.
16. As a developer, I want to expand a tick trace after each tick and read every step the agent took so that I can understand exactly why it made its decision.
17. As a developer, I want `thinking` steps in the trace rendered as readable italic text so that I can follow the agent's reasoning without parsing JSON.
18. As a developer, I want `tool_call` steps in the trace rendered as labelled code blocks showing the tool name, arguments, and result so that I can verify the agent called the right tools with the right inputs.
19. As a developer, I want `fallback` steps rendered as warning boxes so that I immediately notice when the LLM was unavailable and the deterministic policy took over.
20. As a developer, I want `llm_error` steps rendered as error boxes with the error type and message so that I can diagnose network or API failures.
21. As a developer, I want the app to work without reloading the page between ticks so that session state, dock history, and decision history are preserved across the entire simulated trip.
22. As a developer, I want tick history (result + trip state snapshot) to accumulate across ticks so that I can scroll back and compare how the agent's reasoning changed over time.
23. As a developer, I want the app to remain usable after the agent calls `finish_trip` so that I can inspect the final trace without losing the session.
24. As a developer, I want the Run Tick button to be disabled after the trip is finished so that I cannot fire meaningless ticks after the agent has declared the trip over.

---

## Implementation Decisions

### New modules

**Station search module** — A pure, side-effect-free function that accepts a query string and the full station info dict (from `fetch_all_stations()`) and returns a ranked list of matching stations. Matching is case-insensitive substring search over station names. Returns at most 10 results. This is the only logic in the app that is worth testing in isolation.

**Trip state module** — A small set of pure helper functions that manage the `trip_state` dict. The three responsibilities are:

- *Initialise* — build a clean `trip_state` dict from a station ID, station name, and arrival time. Sets `dock_history`, `recent_decisions`, `rejected_station_ids`, `target_just_switched`, and `status` to their starting values.
- *Record observation* — append a `{observed_at, docks_available}` entry to `dock_history` given a live dock count and a timestamp.
- *Record decision* — read the fields the agent mutated on `trip_state` after a tick (`status`, `next_check_seconds`, `next_check_reason`, `alert`, `finish_reason`) and append the corresponding entry to `recent_decisions`.

**Streamlit app** — A single-page app with five panels described below. It is a thin UI layer: all business logic lives in the modules above or in the existing `run_tick()`, `fetch_all_stations()`, and `get_station_status()` functions. No business logic is inline in the app.

### Panel structure

1. **Setup Panel** — Name search text input + live-filtered selectbox of matches. A Look Up button fetches the current live dock count for the selected station. A Start Monitoring button initialises `trip_state` and `tick_history` in Streamlit session state.

2. **ETA Controls** — Four buttons: `10 min away`, `5 min away`, `2 min away`, `Arrived`. The first three update `arrival_time` on `trip_state`; `Arrived` marks the trip finished. The current ETA countdown is shown below the row.

3. **Run Tick + Status Bar** — A prominent Run Tick button. Before calling `run_tick()`, the app fetches a live dock count and records it in `dock_history`. After the call, the app records the agent's decision in `recent_decisions` and appends `(result, trip_state_snapshot)` to `tick_history`. A compact status bar shows target station · live docks · ETA · monitor status · next check interval · tick source.

4. **Alert Panel** — Rendered only when `trip_state["status"] == "alerted"`. Shows headline and message. Each alternative station is a card with a Switch button. Switching updates the target, sets `target_just_switched`, appends the old target to `rejected_station_ids`, and clears the alert.

5. **Trace Expander** — `st.expander("Tick trace")` renders the most recent tick's trace. Trace step types: `thinking` → italic grey text; `tool_call` → labelled code block; `fallback` → warning box; `llm_error` → error box.

### Session state shape

Streamlit session state holds exactly two keys:

```
trip_state   — the dict passed into and mutated by run_tick()
tick_history — list of (result_dict, trip_state_snapshot_dict) tuples
```

(From prototype — this is the minimal shape that supports all five panels.)

### Dock history contract

The app is responsible for populating `dock_history`. Before each tick, it calls `get_station_status()` for the target station and appends `{observed_at: <ISO timestamp>, docks_available: <int>}`. The agent never populates `dock_history` itself.

### Recent decisions contract

The app is responsible for populating `recent_decisions`. After each tick, it reads the fields the agent mutated on `trip_state` and appends one entry of the shape the agent expects: `{action, seconds, reason}` for `set_next_check`, `{action, headline}` for `alert_user`, and `{action, reason}` for `finish_trip`.

### ETA default

When Start Monitoring is clicked, `arrival_time` is initialised to `now + 10 minutes`. The ETA buttons update it from there.

### Auto-poll

Manual-only. The Run Tick button is the only way to fire a tick. No `st.fragment` auto-poll loop.

---

## Testing Decisions

**What makes a good test:** test the function's observable output given its inputs. Do not test internal implementation details or assert on private state. Mock only at the boundary where external I/O (GBFS network calls, the Gemini API) would otherwise make tests slow or flaky.

**Station search module** — Unit tests covering: exact name match, case-insensitive substring match, no matches, multiple matches ranked by match position, query longer than any station name. No mocking needed (pure function).

**Trip state module** — Unit tests covering: `make_initial_trip_state` produces the correct shape and defaults; `record_dock_observation` appends correctly to `dock_history`; `record_tick_decision` produces the correct `recent_decisions` entry for each of the three terminal actions (`set_next_check`, `alert_user`, `finish_trip`); calling `record_tick_decision` after `alert_user` does not overwrite the `alerted` status.

**The Streamlit app itself** — Not unit tested. UI behaviour is verified manually by running the app.

**Prior art** — `tests/tools/test_station_status.py` and `tests/tools/test_station_information.py` use `pytest` and `pytest-mock`. New tests follow the same structure.

---

## Out of Scope

- Auto-polling (`st.fragment` background loop) — manual ticks only for now.
- Voice input or TTS output — this is a text-based debugger.
- Multi-trip history across page reloads — session state is in-memory only.
- Editing `preferences` or `rejected_station_ids` directly in the UI — these are managed implicitly through the Switch flow.
- Authentication or multi-user support.
- Deployment (the app is a local dev tool, not a hosted service).
- Any changes to `run_tick()`, the agent tools, or the GBFS data layer.

---

## Further Notes

- The app lives at `app/streamlit_app.py`. The `app/` directory does not exist yet and must be created.
- The app must add the repo root to `sys.path` so that `src.bikeshare` is importable, consistent with how `scripts/probe.py` handles this.
- `fetch_all_stations()` caches for the process lifetime. The station list will not refresh between ticks unless the page is reloaded, which is acceptable for a dev tool.
- `fetch_live_status()` has a 30-second TTL cache. The live dock count shown in the status bar and pre-tick observation may be up to 30 seconds stale, which is acceptable.
- Streamlit must be added to the project dependencies (`pyproject.toml`).
