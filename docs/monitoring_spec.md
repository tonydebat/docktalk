# DockTalk Monitoring Spec

## Purpose

This document defines how DockTalk monitors the confirmed target station and backup stations after the rider starts a return plan.

The monitor should be quiet by default. It should speak only when the rider needs to act, when the rider asks for an update, or when DockTalk can no longer trust the data.

## Main Principle

The monitor has two jobs:

1. Watch the confirmed target station.
2. Keep backup stations ready in case the target becomes risky.

Target monitoring is rider-facing. Backup monitoring is mostly internal.

## When Monitoring Starts

Monitoring starts only after all of these are true:

- the rider has confirmed a target station
- DockTalk has fetched the latest station status
- DockTalk has stored the target station in session state
- DockTalk has created an initial backup shortlist

Recommended version 1 behavior:

```text
Rider confirms target
-> fetch fresh station status
-> scout backups
-> store monitor state
-> speak confirmation
-> start polling every 60 seconds
```

If the initial station status fetch fails, monitoring should not start yet. DockTalk should tell the rider that live dock data is unavailable and ask them to try again.

## When Monitoring Ends

Monitoring ends when one of these happens:

- the rider says they returned the bike
- the rider cancels monitoring
- the rider changes destination and confirms a new target
- the app cannot refresh station data for too long
- the session is closed

Recommended version 1 rule:

```text
If live station data cannot be refreshed for 10 minutes, stop active monitoring and tell the rider.
```

This is better than pretending the app still knows what is happening.

## Poll Cycle

Use one monitor tick every 60 seconds for version 1.

Bike Share data may refresh more often, but a 60 second tick is easier to demo, easier to debug, and less noisy for the rider.

At each tick:

1. Fetch live station status.
2. If fetch succeeds, update target station status.
3. Refresh backup station status.
4. Replace weak backups if needed.
5. Update estimated time remaining.
6. Classify target risk.
7. Decide whether the rider should be informed.
8. Schedule the next tick.

## Target Station Monitoring

Target monitoring answers this question:

```text
Is the rider's confirmed station still a good return choice?
```

Track these fields:

```json
{
  "target_station_id": "station_123",
  "target_name": "Union Station",
  "available_docks": 2,
  "previous_available_docks": 5,
  "station_status": "active",
  "last_successful_fetch_at": "2026-05-25T17:35:00-04:00",
  "risk_state": "warning"
}
```

Python should handle obvious cases:

| Condition | Action |
|---|---|
| 0 docks | Recommend switch immediately |
| Station offline | Recommend switch immediately |
| Many docks and short ETA | Stay quiet |
| Low docks and unclear context | Ask Gemini for risk evaluation |

## Alternative Station Monitoring

Alternative monitoring answers this question:

```text
If the target becomes bad, what should DockTalk recommend?
```

Backup stations should be scouted immediately after target confirmation and refreshed on every monitor tick.

Track up to 5 backup candidates internally.

Speak up to 3 only when the rider asks for options.

Recommend 1 best backup when switching is needed.

Each backup should track:

```json
{
  "station_id": "station_456",
  "name": "Bay and Front",
  "available_docks": 7,
  "distance_meters": 250,
  "station_status": "active",
  "backup_rank": 1
}
```

During each tick:

1. Refresh dock counts for current backups.
2. Remove backups that are full, offline, or too far away.
3. Search for replacements if fewer than 3 good backups remain.
4. Re-rank the backup list.

DockTalk should not speak just because the backup list changed.

## Risk Evaluation

Risk evaluation should use both rules and Gemini.

Python should decide clear cases. Gemini should evaluate unclear cases where context matters.

Useful context includes:

- current dock count
- previous dock count
- dock count trend
- ETA remaining
- time of day
- weekday, weekend, or holiday
- station area
- backup quality

Suggested risk states:

```text
SAFE
WATCH
WARNING
SWITCH_RECOMMENDED
CRITICAL
DATA_STALE
STOPPED
```

## When To Inform The Rider

Inform the rider when the update is actionable.

Speak when:

- the target becomes full
- the target goes offline
- risk moves from safe or watch to warning
- risk moves from warning to switch recommended
- the current recommended backup becomes risky or full
- station data becomes stale
- the rider asks for an update
- the rider asks for options

Stay quiet when:

- target dock count changes but risk state does not change
- backups change silently
- target remains safe
- the same warning was already spoken recently

Use a cooldown to avoid repeated warnings.

Recommended version 1 rule:

```text
Do not repeat the same spoken warning within 3 minutes.
```

## If Live Data Fails

Do not call Gemini when the station feed fails. Gemini cannot evaluate risk without fresh data.

At the due tick time:

1. Try fetching live station status.
2. If the fetch fails, keep the last known station state.
3. Mark data as stale.
4. Retry after 15 seconds.
5. Try up to 3 quick retries.
6. If all quick retries fail, speak a data stale warning once.
7. Continue retrying every 60 seconds.
8. Stop active monitoring if no fresh data is available for 10 minutes.

Suggested stale warning:

```text
I cannot refresh live dock data right now. The last known status was 2 docks at Union Station. I will keep trying.
```

Suggested stop message:

```text
I still cannot refresh live dock data, so I am stopping active monitoring. Please check the Bike Share app or station screen before returning.
```

## Monitor State Data

Streamlit session state should store the monitor state.

Suggested shape:

```json
{
  "monitor_status": "MONITORING",
  "target_station_id": "station_123",
  "backup_station_ids": ["station_456", "station_789"],
  "started_at": "2026-05-25T17:30:00-04:00",
  "eta_minutes_original": 10,
  "eta_minutes_remaining": 7,
  "last_successful_fetch_at": "2026-05-25T17:33:00-04:00",
  "last_tick_at": "2026-05-25T17:34:00-04:00",
  "next_tick_at": "2026-05-25T17:35:00-04:00",
  "fetch_failure_count": 0,
  "last_spoken_alert_type": "warning",
  "last_spoken_at": "2026-05-25T17:32:00-04:00"
}
```

## Version 1 Defaults

```text
poll_interval_seconds = 60
quick_retry_seconds = 15
max_quick_retries = 3
warning_cooldown_seconds = 180
max_stale_minutes = 10
backup_candidate_count = 5
spoken_option_count = 3
```

## Short Summary

At each monitor tick, DockTalk refreshes live data, updates the target station, refreshes backups, evaluates risk, and speaks only if the rider needs to act or the data can no longer be trusted.
