# DockTalk Streamlit Monitor Debugger

A single-page app at `app/streamlit_app.py` that lets you manually configure a trip, run ticks one at a time, and inspect every input and output.

---

## `run_tick()` Inputs and Outputs

### What Goes In (`trip_state`)

| Field | Type | Notes |
|---|---|---|
| `target_station_id` | str | The station to monitor |
| `target_station_name` | str | Display name, looked up from ID |
| `arrival_time` | datetime | When the rider expects to arrive |
| `preferences` | list | Optional rider preferences |
| `dock_history` | list | `[{observed_at, docks_available}]` — accumulated across ticks |
| `recent_decisions` | list | `[{action, seconds, reason}]` — accumulated across ticks |
| `rejected_station_ids` | list | Stations the rider has refused |
| `target_just_switched` | bool | True on first tick after a switch |

### What Comes Out (result dict + mutated `trip_state`)

| Field | Where | Notes |
|---|---|---|
| `result["source"]` | result | `"llm"` or `"fallback"` |
| `result["trace"]` | result | List of `thinking`, `tool_call`, `fallback`, `llm_error` steps |
| `result["fallback_reason"]` | result | Set if the LLM failed |
| `trip_state["status"]` | trip_state | `"monitoring"`, `"alerted"`, or `"finished"` |
| `trip_state["next_check_seconds"]` | trip_state | Interval set by `set_next_check` |
| `trip_state["next_check_reason"]` | trip_state | Reason text |
| `trip_state["alert"]` | trip_state | `{headline, message, alternatives}` — set by `alert_user` |
| `trip_state["finish_reason"]` | trip_state | Set by `finish_trip` |

---

## App Structure

### Session State

Streamlit session state holds two things:

- `trip_state` — the dict passed into and mutated by `run_tick()`
- `tick_history` — a list of `(result, snapshot_of_trip_state)` tuples, one per tick

---

### 1. Setup Panel

At the top of the page.

- **Station ID text input** — accepts a raw Bike Share Toronto station ID.
- **Name lookup button** — calls `fetch_all_stations()` and `get_station_status()` to resolve the station name and preview its current live dock count.
- **Start Monitoring button** — initialises `trip_state` and resets `dock_history`, `recent_decisions`, and `rejected_station_ids`.

**Open question — station lookup UX:**

| Option | Description |
|---|---|
| A (simplest) | ID-only text input |
| B | Name search text input → fuzzy match over `fetch_all_stations()` → selectbox of matches → confirm |

---

### 2. ETA Controls

A row of four buttons that update `trip_state["arrival_time"]` without running a tick.

| Button | Action |
|---|---|
| `10 min away` | Sets `arrival_time` to `now + 10 minutes` |
| `5 min away` | Sets `arrival_time` to `now + 5 minutes` |
| `2 min away` | Sets `arrival_time` to `now + 2 minutes` |
| `Arrived` | Marks trip as finished (equivalent to `finish_trip`) |

The current ETA countdown is displayed below the buttons and updates on every re-render.

---

### 3. Run Tick Button and Status Bar

- A prominent **▶ Run Monitor Tick** button calls `run_tick(trip_state)`.
- Before calling, the app appends a `dock_history` entry with the current live dock count and timestamp.
- After the call, the app appends to `recent_decisions` and stores the result in `tick_history`.

Below the button, a compact **status bar** shows:

```
Target station · Live docks · ETA · Monitor status · Next check in N seconds · Tick source (LLM / fallback)
```

---

### 4. Alert Panel

Rendered only when `trip_state["status"] == "alerted"`.

- Displays `alert["headline"]` and `alert["message"]`.
- Shows one card per alternative station.
- Each card has a **Switch to this station** button that:
  - Updates `target_station_id` and `target_station_name`
  - Sets `target_just_switched = True`
  - Adds the old target to `rejected_station_ids`
  - Clears the alert from `trip_state`

---

### 5. Trace Expander

An `st.expander("Tick trace")` below the tick button renders each step in `result["trace"]`.

| Step type | Rendered as |
|---|---|
| `thinking` | Italic grey text |
| `tool_call` | Labelled code block showing tool name, args, and result |
| `fallback` | Warning box with reason and observed values |
| `llm_error` | Error box with error type and message |

---

## Open Questions

1. **Dock history source:** should the app pull a live dock count from `get_station_status()` before calling `run_tick()` to populate `dock_history`, or should it trust what `predict_fill_probability` observed inside the tick?

2. **Auto-polling vs. manual ticks:** the plan above is manual (one button press = one tick), which is easiest to debug. An optional auto-poll toggle using `st.fragment` + `time.sleep(next_check_seconds)` could run ticks automatically at the agent-chosen interval.

