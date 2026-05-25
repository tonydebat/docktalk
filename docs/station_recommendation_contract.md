# Station Recommendation Contract

## Purpose

This document defines what DockTalk must know before it recommends a station to the rider.

The key user experience rule is:

```text
Do not speak only the station name. Also give a short location cue.
```

Example:

```text
Best choice is Union Station, near Front Street and Bay Street. It has 4 open docks.
```

This helps the rider understand where the station is without opening a map.

## Recommendation Object

Every target station and backup station should use this shape:

```json
{
  "station_id": "station_123",
  "name": "Union Station",
  "location_hint": "near Front Street and Bay Street",
  "available_docks": 4,
  "distance_meters": 180,
  "station_status": "active",
  "recommendation_reason": "closest active station with enough docks"
}
```

## Required Fields

| Field | Required | Purpose |
|---|---|---|
| station_id | Yes | Stable ID for monitoring and tool calls |
| name | Yes | Official station name |
| location_hint | Yes | Short spoken cue for where the station is |
| available_docks | Yes | Current live dock count |
| distance_meters | Recommended | Helps explain alternatives |
| station_status | Yes | Avoid recommending offline stations |
| recommendation_reason | Recommended | Helps Gemini explain the choice |

## Location Hint Rules

Use the best available source in this order:

1. Official station name if it already contains an intersection.
2. Official station address if available in the feed.
3. A small manually curated alias map for demo stations.
4. A simple phrase based on the matched destination, such as "near Union Station".

Do not let Gemini invent an intersection.

If Python does not have a reliable street cue, use a weaker but honest phrase:

```text
near Union Station
```

instead of:

```text
near Front Street and Bay Street
```

## Spoken Recommendation Format

For the main recommendation:

```text
Best choice is {station_name}, {location_hint}. It has {available_docks} open docks.
```

Example:

```text
Best choice is Union Station, near Front Street and Bay Street. It has 4 open docks.
```

For a backup recommendation:

```text
Switch to {station_name}, {location_hint}. It has {available_docks} open docks and is about {distance} meters away.
```

Example:

```text
Switch to Bay Street and Front Street. It has 7 open docks and is about 250 meters away.
```

For multiple options:

```text
Your options are Bay and Front with 7 docks, Wellington and York with 5 docks, and Simcoe and Front with 3 docks.
```

If the rider is moving, keep spoken output short. The location cue should help, not turn into navigation.

## Gemini Role

Gemini may rewrite station names into natural spoken language.

Gemini may choose the clearest wording from fields provided by Python.

Gemini must not:

- invent station names
- invent intersections
- invent dock counts
- invent distances
- recommend a station not included in the candidate list

## Python Role

Python should prepare the station objects before Gemini writes the spoken response.

Python should:

- fetch station metadata and live status
- compute or copy `location_hint`
- compute distance when possible
- remove offline or full stations from backup candidates
- pass only real candidate stations to Gemini

## Version 1 Default

For version 1, do not add reverse geocoding unless it is already easy.

Use official station names, official address fields if available, and a small curated alias map for demo-critical stations.

This keeps the demo reliable and avoids a new dependency.
