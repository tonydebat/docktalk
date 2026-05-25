# DockTalk Rider Journey

## Purpose

DockTalk helps a Bike Share Toronto rider return a bike without getting stranded at a full station.

The main job is not general station search. The main job is to help the rider choose a return station, quietly monitor it while they are on the way, and speak up only when the rider may need to change plans.

## Version 1 User Story

As a Bike Share Toronto rider, I want to tell DockTalk where I plan to return my bike, so that it can recommend a good station, monitor dock availability while I am riding, and warn me if I should switch to a safer nearby station.

## Core Product Rule

Quiet monitoring, actionable alerts.

DockTalk should not speak every time dock counts change. It should inform the rider only when the update helps them decide what to do next.

## High-Level Journey

1. The rider speaks a return request.
2. Whisper converts the voice message into text.
3. Gemini analyzes the text and extracts the rider's destination, intent, and optional ETA.
4. Python tools fetch Bike Share Toronto station data and live dock availability.
5. DockTalk recommends the best station, or up to 3 good options.
6. The recommendation is spoken back to the rider.
7. The rider confirms the station they want to monitor.
8. DockTalk starts monitoring the confirmed station.
9. DockTalk quietly scouts and refreshes backup stations.
10. If the chosen station becomes risky, DockTalk evaluates whether the rider should be informed.
11. If the risk is actionable, DockTalk warns the rider or recommends switching.
12. The rider can ask for updates, hear options, switch stations, change destination, or stop monitoring.

## Setup Flow

### 1. Rider Starts With Voice

Example rider phrase:

```text
I need to return near Union Station in about 10 minutes.
```

### 2. Whisper Transcribes Speech

Whisper converts the rider's voice into text.

Example output:

```text
I need to return near Union Station in about 10 minutes.
```

### 3. Gemini Parses The Request

Gemini extracts structured intent from the transcript.

Example output:

```json
{
  "intent": "return_bike",
  "destination": "Union Station",
  "eta_minutes": 10
}
```

### 4. Python Fetches Station Data

Python calls the Bike Share Toronto GBFS feeds to get:

- station names
- station locations
- live available dock counts
- station status

### 5. DockTalk Recommends Stations

Python ranks real stations using distance, available docks, and station status.

Gemini may help phrase the recommendation, but it should choose only from real station data provided by Python.

See `docs/station_recommendation_contract.md` for the station fields used in spoken recommendations.

Example spoken response:

```text
Best choice is Union Station, near Front Street and Bay Street. It has 4 open docks. I can also watch nearby backups.
```

### 6. Rider Confirms

Example rider phrase:

```text
Use Union Station.
```

DockTalk confirms:

```text
Okay. I will monitor Union Station.
```

## Monitoring Flow

After confirmation, Streamlit owns the monitor loop.

Gemini should not run the loop. Python and Streamlit should handle polling, state, and basic rules. Gemini should be called only when language understanding or contextual judgment is useful.

See `docs/monitoring_spec.md` for the detailed monitoring rules.

### Monitor Loop

Every 30 to 60 seconds:

1. Fetch fresh station status from the Bike Share API.
2. Update the target station's dock count.
3. Refresh backup station dock counts.
4. Update estimated time remaining.
5. Classify the target station as safe, watch, warning, switch recommended, or critical.
6. Decide whether the rider needs to hear an update.

## Contextual Risk Evaluation

Risk depends on more than current dock count.

Useful signals include:

- current available docks
- previous available docks
- change since last poll
- estimated time to arrival
- time of day
- weekday, weekend, or holiday
- station area, such as downtown transit hub or quieter neighborhood
- quality of nearby backup stations

Python should decide obvious cases directly.

Examples:

- 0 docks means alert immediately.
- Station offline means alert immediately.
- Many docks and short ETA means stay quiet.

Gemini should help when the situation needs context.

Examples:

- 2 docks left, 10 minutes away, weekday evening near Union Station
- 1 dock left, 2 minutes away, but alternatives are far away
- 3 docks left, 15 minutes away, and dock count is dropping quickly

Gemini should return structured output.

Example:

```json
{
  "decision": "recommend_switch",
  "risk_level": "high",
  "risk_score": 0.82,
  "reason": "The target has only 2 docks, dock count is dropping, and this is a weekday evening near a major transit hub.",
  "recommended_station_id": "station_123",
  "spoken_message": "Union Station is getting risky. Switch to Bay and Front, which has 7 open docks nearby."
}
```

## Alternative Station Scouting

Alternative scouting should happen quietly in the background.

The scout is a Python workflow, not a separate AI agent.

### Who Does What

| Task | Owner |
|---|---|
| Fetch stations | Python |
| Fetch live dock counts | Python |
| Find nearby stations | Python |
| Score and rank backup stations | Python |
| Evaluate contextual risk in unclear cases | Gemini |
| Generate spoken explanation | Gemini |
| Speak message to rider | Streamlit app / speech layer |

### When Scouting Happens

Scouting happens at three points:

1. After the rider confirms a target station.
2. During every monitor poll.
3. When the target station becomes risky or full.

### How Scouting Works

The scout:

1. Starts from the confirmed target station.
2. Searches nearby Bike Share stations within a distance limit.
3. Removes invalid stations.
4. Scores the remaining stations.
5. Stores the best backup candidates.

Invalid stations include:

- the current target station
- offline stations
- stations with 0 available docks
- stations too far away

Suggested version 1 rule:

```text
Track up to 5 backup candidates internally.
Speak up to 3 options when the rider asks.
Recommend 1 best station when action is needed.
```

Do not always force exactly 3 alternatives. If only 1 or 2 good alternatives exist, DockTalk should say only those.

### If A Backup Becomes Full

The backup list is a living shortlist.

During each monitor poll:

1. Refresh dock counts for current backups.
2. Remove backups that are full or offline.
3. Search for replacement backups if too few good options remain.
4. Re-rank the backup list.

DockTalk should not tell the rider every time this internal list changes.

## When The Rider Should Be Informed

The rider should be informed when an update is actionable.

DockTalk should speak when:

- the target station becomes full
- the target station goes offline
- risk moves from safe or watch to warning
- risk moves from warning to switch recommended
- the station DockTalk already recommended becomes risky or full
- the rider asks for an update
- the rider asks for options

DockTalk should stay quiet when:

- dock count changes but risk level does not change
- backup list changes silently
- target station remains safe
- the same warning was already spoken recently

Suggested alert rule:

```text
Speak only if the rider may need to change behavior.
```

## Limited Rider Commands

Version 1 should support a small command set.

DockTalk should not behave like a general chatbot during monitoring.

| Command | Example phrase | Action |
|---|---|---|
| Get update | Any update? | Speak current target status and risk |
| Hear options | What are my options? | Speak up to 3 current backup stations |
| Switch station | Switch to Bay and Front | Set that station as the new target after confirmation |
| Change destination | Change destination to Union Station | Restart station recommendation flow |
| Stop monitoring | Cancel / I returned the bike | End monitoring |

Unknown commands should get a bounded response:

```text
I can help with updates, options, switching stations, changing destination, or stopping monitoring.
```

## State Model

Suggested states:

```text
NOT_STARTED
AWAITING_CONFIRMATION
MONITORING_SAFE
MONITORING_WATCH
MONITORING_WARNING
SWITCH_RECOMMENDED
STOPPED
```

## State Transitions

| Current state | Event | Internal action | Rider message | Next state |
|---|---|---|---|---|
| NOT_STARTED | Rider gives destination | Parse request, fetch stations, recommend target | Speak best station or options | AWAITING_CONFIRMATION |
| AWAITING_CONFIRMATION | Rider confirms target | Store target, scout backups, start polling | Confirm monitoring started | MONITORING_SAFE |
| MONITORING_SAFE | Target remains safe | Refresh target and backups | Stay quiet | MONITORING_SAFE |
| MONITORING_SAFE | Risk increases slightly | Refresh backups, update risk | Stay quiet or brief warning | MONITORING_WATCH |
| MONITORING_WATCH | Risk becomes meaningful | Ask Gemini if needed, prepare warning | Speak warning if actionable | MONITORING_WARNING |
| MONITORING_WARNING | Risk becomes high | Re-rank backups, choose best switch | Recommend switch | SWITCH_RECOMMENDED |
| Any monitoring state | Target full or offline | Re-rank backups immediately | Recommend switch or alert | SWITCH_RECOMMENDED |
| SWITCH_RECOMMENDED | Rider accepts switch | Set recommended station as new target, scout new backups | Confirm new station monitoring | MONITORING_SAFE |
| Any monitoring state | Rider asks for update | Summarize target status | Speak current status | Same state |
| Any monitoring state | Rider asks for options | Read current backups | Speak up to 3 options | Same state |
| Any monitoring state | Rider changes destination | Pause old monitor, restart station matching | Ask for confirmation | AWAITING_CONFIRMATION |
| Any monitoring state | Rider cancels or says bike returned | Stop polling | Confirm monitoring stopped | STOPPED |

## Version 1 Boundaries

In scope:

- voice input through Whisper
- Gemini request parsing
- real Bike Share Toronto GBFS data
- station recommendation
- rider confirmation
- Streamlit monitor loop
- quiet backup scouting
- contextual risk evaluation for unclear cases
- spoken warnings and switch recommendations
- limited rider commands

Out of scope for version 1:

- general chatbot behavior
- automatic route navigation
- user accounts
- prediction model training
- automatic switching without rider confirmation
- speaking every dock count change

## One-Sentence Summary

DockTalk lets a rider choose a return station by voice, quietly watches whether that station is still a good choice, keeps backup stations ready, and speaks only when the rider needs to act.
