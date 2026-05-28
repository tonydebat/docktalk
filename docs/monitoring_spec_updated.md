# DockTalk Monitoring Spec

This document describes monitoring from the rider journey perspective, while
also defining the Python, Gemini, Streamlit, and voice responsibilities.

## Core Idea

After the rider vocally confirms a target station, DockTalk quietly monitors the
station until one of these happens:

1. The station becomes risky and the rider needs an alert.
2. The rider switches to an alternative station.
3. The rider confirms they are done or cancels.
4. The app checks in because the estimated arrival time is long past.

DockTalk should stay quiet unless the rider needs to act.

## Responsibility Split

```text
Python:
owns trip_state, fetches GBFS data, updates dock_history, runs tools, handles
lifecycle states, and controls the Streamlit scheduling gate.

Gemini:
chooses between set_next_check and alert_user during an active monitor tick.

Voice/UI:
speaks alerts and check-ins, listens for rider responses, and turns speech into
small command objects.

Streamlit:
reruns the app, renders current state, and provides a heartbeat for monitoring.
```

Gemini should not be the main owner of trip completion. Trip completion is
better handled by Python and rider confirmation.

## Monitoring Starts

Monitoring starts only after the rider has confirmed a target station.

At that point Python creates `trip_state`:

```python
trip_state = {
    "target_station_id": station_id,
    "target_station_name": station_name,
    "eta_source": eta_source,
    "minutes_to_arrival": minutes_to_arrival,
    "arrival_time": arrival_time,
    "dock_history": [],
    "recent_decisions": [],
    "status": "monitoring",
    "alert": None,
}
```

Use `minutes_to_arrival` for UI clarity. Use `arrival_time` for time checks.
The arrival time is only an estimate, so it should not automatically end the
trip.

## Status Values

Keep the status values small:

```python
VALID_TRIP_STATUSES = {
    "monitoring",
    "alerted",
    "check_in",
    "finished",
}
```

Meaning:

```text
monitoring:
normal background dock monitoring is active

alerted:
dock-risk alert has been created; wait for rider response

check_in:
ETA grace period has passed; ask if the rider is still riding

finished:
stop monitoring
```

## Streamlit Heartbeat Versus Monitor Tick

Streamlit reruns the app. That is not the same thing as running the agent.

Use Streamlit as a heartbeat, then use Python to decide whether a monitor tick
is actually due.

Example shape:

```python
@st.fragment(run_every=30)
def monitor_fragment() -> None:
    trip_state = st.session_state["trip_state"]

    if trip_state["status"] != "monitoring":
        return

    if datetime.now() < trip_state["next_check_at"]:
        return

    run_monitor_tick(trip_state)
```

This allows Gemini to say `set_next_check(seconds=120)` while Streamlit still
wakes every 30 seconds. Streamlit wakes up, but Python skips the agent until
`next_check_at` has arrived.

## One Monitor Tick

One monitor tick is one agent wake-up.

Recommended order:

```text
1. Python wakes up for a monitor tick.
2. Python checks lifecycle conditions, such as rider cancellation or check-in due.
3. Python fetches current target station status from GBFS.
4. Python appends the latest dock count to dock_history.
5. Python sends updated trip_state, system prompt, and tool catalog to Gemini.
6. Gemini picks tools to call, with arguments.
7. Python runs the actual tool calls from tools.py.
8. Python sends tool results back to Gemini.
9. Gemini chooses set_next_check or alert_user.
10. Python updates trip_state based on the chosen action.
11. Streamlit renders the updated state.
```

Clean mental model:

```text
Python observes.
Gemini reasons.
Python executes.
Streamlit renders.
Voice speaks when needed.
```

## Two Event Paths

DockTalk has two event paths that update the same `trip_state`.

```text
Path A:
scheduled monitor tick

Path B:
rider command
```

Both paths begin with rider intent. The rider starts monitoring by confirming a
target station. After that, individual monitor ticks are app-scheduled, while
rider commands are immediate rider-initiated interrupts.

### Normal Monitoring Path

This path runs when Streamlit wakes up and Python decides the next monitor check
is due.

```text
Streamlit heartbeat
-> Python checks whether next_check_at is due
-> Python fetches target station dock status
-> Python appends observation to dock_history
-> Python sends updated trip_state, prompt, and tool catalog to Gemini
-> Gemini calls evidence tools if needed
-> Python runs tool calls and returns results
-> Gemini chooses set_next_check or alert_user
-> Python updates trip_state
-> Streamlit renders the updated UI
-> Voice speaks only if there is an alert or rider-facing message
```

Use this path for autonomous monitoring.

### Rider Command Path

This path runs when the rider speaks or taps a command while monitoring is
active.

```text
Rider speaks or taps
-> Voice/UI captures the command
-> Whisper turns speech into text if voice was used
-> Command parser turns text into a small intent object
-> Python handles the command immediately
-> Python updates trip_state
-> Streamlit renders the updated UI
-> Voice speaks the response if needed
```

Use this path for ad hoc rider requests:

```text
How is my station looking?
What are my other options?
Cancel monitoring.
I'm done.
Change stations.
```

Rider commands are interrupts, but they are not exceptions. They are explicit
state transitions on the same `trip_state`.

## App Loop Priority

Rider commands should take priority over scheduled monitor ticks.

Recommended priority order:

```text
1. Handle any new rider command first.
2. If status is alerted, wait for alert response.
3. If status is check_in, wait for check-in response.
4. If status is finished, do nothing except render final state.
5. If status is monitoring and next_check_at is due, run one monitor tick.
6. Otherwise, render current state and wait.
```

Pseudo-code:

```python
def app_loop() -> None:
    trip_state = st.session_state["trip_state"]

    command = get_pending_voice_or_button_command()
    if command:
        handle_rider_command(command, trip_state)
        render_trip_state(trip_state)
        return

    if trip_state["status"] in {"alerted", "check_in", "finished"}:
        render_trip_state(trip_state)
        return

    if (
        trip_state["status"] == "monitoring"
        and datetime.now() >= trip_state["next_check_at"]
    ):
        run_monitor_tick(trip_state)

    render_trip_state(trip_state)
```

This prevents cases like running a Gemini risk check after the rider has already
said "cancel monitoring."

## Next Check Timing Rules

`next_check_seconds` and `next_check_at` only matter when:

```python
trip_state["status"] == "monitoring"
```

When the app is waiting for the rider, autonomous monitor ticks should pause.

Paused statuses:

```python
{"alerted", "check_in", "finished"}
```

In these statuses, Streamlit may still rerun, but Python should not run the
monitor agent.

Clean invariant:

```python
if trip_state["status"] == "monitoring":
    trip_state["next_check_at"] should exist

if trip_state["status"] in {"alerted", "check_in", "finished"}:
    scheduled monitor ticks should not run
```

### Rider Command Timing

When a rider command arrives, timing depends on the command.

For an informational command, keep monitoring active and usually keep the
existing schedule:

```text
get_update:
fetch and speak current status, keep existing next_check_at

show_options:
fetch and speak nearby options, keep existing next_check_at
```

For a command that changes the target, return to monitoring with a short next
check:

```python
trip_state["status"] = "monitoring"
trip_state["next_check_seconds"] = 20
trip_state["next_check_reason"] = "target changed - confirm new station still has docks"
trip_state["next_check_at"] = datetime.now() + timedelta(seconds=20)
```

For a command that ends monitoring, timing no longer matters:

```python
trip_state["status"] = "finished"
trip_state["finish_reason"] = "rider cancelled monitoring"
```

### Alert Timing

While `status == "alerted"`, do not set a new monitor schedule just to keep the
loop moving. Wait for the rider's alert response.

After the rider responds:

```text
switch to alternative:
status = monitoring
next_check_seconds = 20

keep original target:
status = monitoring
next_check_seconds = 20

cancel:
status = finished
```

The short 20 second check after switch or keep-target is deliberate. It confirms
the selected station is still viable after the rider's decision.

### Check-In Timing

While `status == "check_in"`, do not set a new monitor schedule just to keep the
loop moving. Wait for the rider's check-in response.

After the rider responds:

```text
still riding:
status = monitoring
next_check_seconds = 60

stop monitoring:
status = finished
```

The normal 60 second check after "still riding" is enough because this is a
lifecycle check-in, not a dock-risk alert.

## Dock History

`dock_history` comes from Python, not Gemini.

At each monitor tick, before Gemini is called, Python fetches the latest target
station status and appends it:

```python
trip_state["dock_history"].append({
    "observed_at": status["observed_at"],
    "docks_available": status["num_docks_available"],
    "station_status": status["station_status"],
    "is_returning": status["is_returning"],
})
```

This gives Gemini a trend, not just one live number.

Example:

```python
"dock_history": [
    {"observed_at": "tick-1", "docks_available": 8},
    {"observed_at": "tick-2", "docks_available": 5},
    {"observed_at": "tick-3", "docks_available": 2},
]
```

## Recent Decisions

`recent_decisions` is Python's short memory for what already happened.

It should include final monitor actions and rider responses:

```python
trip_state["recent_decisions"].append({
    "action": "set_next_check",
    "seconds": 60,
    "reason": "stable dock availability",
})
```

```python
trip_state["recent_decisions"].append({
    "action": "switch_target",
    "from_station_id": old_station_id,
    "to_station_id": new_station_id,
    "reason": "rider accepted alert alternative",
})
```

This prevents Gemini from acting like every tick is brand new.

## Tool Calling

Gemini chooses tools. Python runs tools.

The tool loop is:

```text
1. Python sends Gemini the tool catalog.
2. Gemini asks to call a tool.
3. Python calls tools.dispatch(name, args, trip_state).
4. Python sends the result back to Gemini.
5. Gemini may call another tool.
6. Gemini eventually calls an action tool.
```

The current monitor agent should normally end a tick with one of:

```text
set_next_check
alert_user
```

`finish_trip` can exist as a safety tool, but normal trip completion should be
owned by Python and rider confirmation.

## Predict Fill Probability

`predict_fill_probability()` answers:

```text
Given the station right now, how likely is it that there will be fewer than
2 docks when the rider arrives?
```

The tool first checks live station health:

```text
station_status
is_returning
num_docks_available
capacity
```

If the station is offline or not accepting returns, prediction is skipped and
the risk is treated as high.

If the station is usable, the rule-based predictor estimates future docks:

```python
predicted_docks = current_docks + drift * minutes_ahead
```

Sign convention:

```text
positive drift:
open docks are increasing

negative drift:
open docks are decreasing because the station is filling with returned bikes
```

Example:

```python
current_docks = 5
minutes_ahead = 10
drift = -0.25
predicted_docks = 5 + (-0.25 * 10)
predicted_docks = 2.5
```

The predictor maps predicted docks to risk:

```text
under 0.5 docks -> 0.90 risk
under 1.5 docks -> 0.70 risk
under 3.0 docks -> 0.45 risk
under 5.0 docks -> 0.20 risk
5 or more docks -> 0.05 risk
```

The predictor is a transparent heuristic, not a trained model. That is okay for
the hackathon demo as long as we describe it honestly.

## Set Next Check

`set_next_check()` means:

```text
Do not alert the rider yet. Keep watching. Wake up again after N seconds.
```

Python updates:

```python
trip_state["next_check_seconds"] = args["seconds"]
trip_state["next_check_reason"] = args["reason"]
trip_state["next_check_at"] = datetime.now() + timedelta(seconds=args["seconds"])
trip_state["status"] = "monitoring"
```

Example policy:

```text
Plenty of docks:
check again in 60 seconds

Low docks:
check again in 30 seconds

Zero docks but rider is still far away:
check again in 20 seconds

Very risky and rider is close:
call alert_user instead
```

`next_check_seconds` does not schedule anything by itself. The Streamlit
heartbeat must check `next_check_at` and skip ticks until it is due.

## Alert User

`alert_user()` is the handoff from monitor agent to voice/UI.

It does not speak, play audio, listen, or change stations. It only stores an
alert in `trip_state`.

Gemini calls:

```python
alert_user(
    headline="Destination is filling up.",
    message="York St / Queen St W has only 1 dock open.",
    alternatives=[
        {
            "station_id": "7001",
            "station_name": "Bay St / Queen St W",
            "docks_available": 6,
            "reason": "Close by with more open docks",
        }
    ],
)
```

Python stores:

```python
trip_state["alert"] = args
trip_state["status"] = "alerted"
```

Then the current Streamlit execution can render the alert immediately. If not,
the next rerun will render it.

Voice/UI should:

```text
1. Show the alert text on screen.
2. Speak the alert.
3. Present the recommended alternative stations.
4. Listen for the rider response.
```

Example spoken alert:

```text
York Street at Queen is filling up. It has only one dock open. I found a safer
option nearby: Bay Street at Queen has six docks. Do you want to switch?
```

## Alternative Stations

Alternatives should be fetched live when an alert is needed.

Gemini normally calls:

```python
get_nearby_stations(station_id, radius_m=800)
```

Python then:

```text
1. Gets target station lat/lon.
2. Gets all station lat/lon values.
3. Gets live dock status for all stations.
4. Excludes the target station.
5. Excludes stations not accepting returns.
6. Excludes stations with too few docks.
7. Calculates distance from the target station.
8. Keeps stations within the radius.
9. Sorts by distance.
10. Enriches candidates with walking time and station context.
```

For demo clarity, setup may also scout backup stations after target
confirmation. But alert recommendations should use fresh live data.

## Rider Response To Alert

After an alert, the rider chooses:

```text
stay with the same target station
switch to an alternative station
cancel monitoring
```

The voice layer should return a small command object.

Examples:

```python
{"intent": "switch_station", "alternative_index": 0}
```

```python
{"intent": "keep_target"}
```

```python
{"intent": "cancel_monitoring"}
```

Accepted voice examples:

```text
yes
switch
option one
go there
no
keep watching
stay with this one
stop monitoring
```

### If Rider Switches

Python updates the target:

```python
chosen = trip_state["alert"]["alternatives"][alternative_index]
old_station_id = trip_state["target_station_id"]

trip_state["target_station_id"] = chosen["station_id"]
trip_state["target_station_name"] = chosen["station_name"]
trip_state["dock_history"] = []
trip_state["recent_decisions"].append({
    "action": "switch_target",
    "from_station_id": old_station_id,
    "to_station_id": chosen["station_id"],
    "reason": "rider accepted alert alternative",
})
trip_state["alert"] = None
trip_state["status"] = "monitoring"
trip_state["target_just_switched"] = True
trip_state["next_check_seconds"] = 20
trip_state["next_check_reason"] = "target switched - confirm new station still has docks"
trip_state["next_check_at"] = datetime.now() + timedelta(seconds=20)
```

The next monitor tick sends Gemini the new target station.

### If Rider Keeps Original Target

Python keeps the same target and resumes monitoring:

```python
trip_state["recent_decisions"].append({
    "action": "keep_target",
    "station_id": trip_state["target_station_id"],
    "reason": "rider rejected alert alternative",
})
trip_state["alert"] = None
trip_state["status"] = "monitoring"
trip_state["next_check_seconds"] = 20
trip_state["next_check_reason"] = "rider kept target after alert"
trip_state["next_check_at"] = datetime.now() + timedelta(seconds=20)
```

The next Gemini call receives the same target plus the rider's decision in
`recent_decisions`.

### If Rider Cancels

Python ends monitoring:

```python
trip_state["alert"] = None
trip_state["status"] = "finished"
trip_state["finish_reason"] = "rider cancelled monitoring"
```

This does not need Gemini.

## Rider Commands During Monitoring

The rider can speak commands even when DockTalk has not just issued an alert.
These are rider-initiated commands, not monitor-agent actions.

Examples:

```text
How is my station looking?
Any update?
How many docks are there?
What are my other options?
Cancel monitoring.
Stop watching.
I'm done.
Switch stations.
```

The voice layer should turn these into small command objects.

Recommended command shapes:

```python
{"intent": "get_update"}
```

```python
{"intent": "show_options"}
```

```python
{"intent": "cancel_monitoring"}
```

```python
{"intent": "finish_trip"}
```

```python
{"intent": "change_target"}
```

These commands do not need to wait for Gemini unless the app needs natural
language wording. Python should handle the state change or data fetch directly.

### Get Update

If the rider asks for an update, Python should fetch current target station
status and respond with the latest known situation.

Suggested behavior:

```text
1. Fetch current target station status.
2. Append the observation to dock_history.
3. Speak a short status update.
4. Keep status as monitoring.
```

Example response:

```text
York Street at Queen has 5 open docks right now. I am still watching it.
```

If the station is risky, the update can include that:

```text
York Street at Queen has only 1 dock open. I recommend switching if you are
close.
```

### Show Options

If the rider asks for other options, Python should call `get_nearby_stations`
and show or speak up to 3 alternatives.

This is not necessarily an alert. The rider asked for options, so it is safe to
speak even if the target is not yet risky.

Example response:

```text
Nearby options are Bay Street at Queen with 6 docks, University Avenue at Queen
with 4 docks, and Chestnut Street with 3 docks.
```

### Cancel Monitoring

If the rider says cancel or stop watching, Python should finish immediately:

```python
trip_state["status"] = "finished"
trip_state["finish_reason"] = "rider cancelled monitoring"
```

No Gemini call is needed.

### Finish Trip

If the rider says they are done or returned the bike, Python should finish
immediately:

```python
trip_state["status"] = "finished"
trip_state["finish_reason"] = "rider confirmed trip finished"
```

No Gemini call is needed.

### Change Target

If the rider asks to change target station, the app should return to the station
selection flow, not continue the current monitor tick.

Recommended behavior:

```text
1. Stop the current monitoring session.
2. Ask for the new destination or station.
3. Match and confirm the new station.
4. Create a new monitoring state for the new target.
5. Resume monitoring with the new target.
```

Python should record the change:

```python
trip_state["status"] = "finished"
trip_state["finish_reason"] = "rider requested a different target"
trip_state["change_target_requested"] = True
trip_state["recent_decisions"].append({
    "action": "change_target_requested",
    "from_station_id": trip_state["target_station_id"],
    "reason": "rider requested a different target",
})
```

## Check-In After ETA Grace Period

Do not automatically end monitoring when the estimated arrival time passes.
The ETA may be wrong.

Instead, after a grace period, Python should ask the rider whether they are
still riding.

Recommended default:

```python
CHECK_IN_GRACE_MINUTES = 30
```

If the rider has not cancelled and the estimated arrival time passed more than
30 minutes ago:

```python
trip_state["status"] = "check_in"
trip_state["check_in"] = {
    "reason": "arrival estimate passed 30 minutes ago",
    "message": "Are you still riding, or should I stop monitoring?",
}
```

Suggested spoken message:

```text
Are you still riding to York Street at Queen, or should I stop monitoring?
```

If rider says keep monitoring:

```python
trip_state["status"] = "monitoring"
trip_state["next_check_seconds"] = 60
trip_state["next_check_reason"] = "rider confirmed still riding"
trip_state["next_check_at"] = datetime.now() + timedelta(seconds=60)
```

If rider says stop:

```python
trip_state["status"] = "finished"
trip_state["finish_reason"] = "rider confirmed trip finished"
```

This is lifecycle logic and should be owned by Python, not Gemini.

## Data Failure

Do not call Gemini when the station feed fails. Gemini cannot evaluate risk
without fresh station data.

At a due tick:

```text
1. Try fetching live station status.
2. If fetch fails, keep the last known station state.
3. Mark data as stale.
4. Retry after 15 seconds.
5. Try up to 3 quick retries.
6. If all quick retries fail, speak a data stale warning once.
7. Continue retrying every 60 seconds.
8. Stop active monitoring if no fresh data is available for 10 minutes.
```

Suggested stale warning:

```text
I cannot refresh live dock data right now. The last known status was 2 docks at
Union Station. I will keep trying.
```

## Version 1 Defaults

```text
heartbeat_seconds = 30
default_next_check_seconds = 60
urgent_next_check_seconds = 20
quick_retry_seconds = 15
max_quick_retries = 3
warning_cooldown_seconds = 180
max_stale_minutes = 10
check_in_grace_minutes = 30
backup_candidate_count = 5
spoken_option_count = 3
max_tool_calls_per_tick = 5
llm_timeout_ms = 60000
```

## Short Summary

The rider confirms a target station. Python creates `trip_state`. Streamlit
wakes on a heartbeat, but Python only runs the monitor tick when `next_check_at`
is due. Python fetches live station data and updates `dock_history` before
Gemini sees the state. Gemini chooses whether to keep monitoring or alert the
rider. Python executes the action. Voice/UI handles alert and check-in
responses. Python owns trip lifecycle and final completion.
