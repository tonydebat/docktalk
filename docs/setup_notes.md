# Setup Notes

These notes describe the simplified setup flow before DockTalk enters the
monitoring loop.

## Goal

Get three pieces of information before monitoring starts:

1. The rider's target return station.
2. The rider's estimated arrival time.
3. Permission to use browser location only if the rider does not know the ETA.

Once those are known, Python creates `trip_state` and passes it into the monitor
loop.

## Voice Input

Use Whisper API for rider speech input.

The setup flow should accept short, natural answers. The rider should not need
to speak in a strict command format.

Examples:

```text
I need to return near City Hall.
Union Station.
Ten minutes.
Use my location.
I don't know.
```

## ETA Prompt

After the target station is selected, DockTalk asks for the ETA.

```text
I can estimate ride time from your location, or you can say a time.
```

Visible UI options:

```text
Use my location | 5 min | 10 min | 15 min | 20 min
```

Accepted voice answers:

```text
use my location
five minutes
ten minutes
fifteen minutes
twenty minutes
I don't know
```

## ETA Decision Logic

Use self-reported ETA first.

If the rider gives a time:

```python
eta_source = "self_reported"
minutes_to_arrival = 10
```

If the rider says "use my location":

```python
eta_source = "geolocation"
minutes_to_arrival = estimate_from_browser_location()
```

If the rider says "I don't know", do not silently use location. Ask permission
first:

```text
No problem. Can I use your location to estimate your arrival time?
```

If the rider agrees:

```python
eta_source = "geolocation"
minutes_to_arrival = estimate_from_browser_location()
```

If the rider declines:

```python
eta_source = "default"
minutes_to_arrival = 10
```

The default should be visible in the UI so the rider knows what the app assumed.

## Geolocation ETA Implementation

When `eta_source = "geolocation"`, the app needs the rider's current browser
location.

The target station location is already known from GBFS station metadata:

```python
target_lat = station["lat"]
target_lon = station["lon"]
```

The missing values are the rider's current coordinates:

```python
rider_lat = ...
rider_lon = ...
accuracy_m = ...
```

Use browser JavaScript to request the phone's location:

```javascript
navigator.geolocation.getCurrentPosition(
  (position) => {
    const riderLat = position.coords.latitude;
    const riderLon = position.coords.longitude;
    const accuracyM = position.coords.accuracy;
  },
  (error) => {
    // Fall back to asking the rider for a time.
  }
);
```

Implementation split:

```text
Browser JavaScript:
gets rider_lat, rider_lon, and accuracy_m from the phone/browser

Python:
already has target_lat and target_lon from GBFS
calculates distance and rough bike ETA
```

Python can estimate distance using `haversine_m()` from
`src/bikeshare/station_data.py`:

```python
distance_m = haversine_m(rider_lat, rider_lon, target_lat, target_lon)
```

Then convert distance to a rough bike ETA:

```python
import math

biking_speed_m_per_min = 200  # about 12 km/h
minutes_to_arrival = max(1, math.ceil(distance_m / biking_speed_m_per_min))
```

If geolocation fails, is denied, or has poor accuracy, fall back to asking the
rider for a self-reported ETA:

```text
I could not get a reliable location. How many minutes away are you?
```

Browser geolocation usually requires HTTPS, except on `localhost`. Local
development should work, but a deployed demo should use an HTTPS URL.

## Minimal Trip State

For the simplified app, keep `trip_state` small:

```python
trip_state = {
    "target_station_id": station_id,
    "target_station_name": station_name,
    "eta_source": eta_source,
    "minutes_to_arrival": minutes_to_arrival,
    "dock_history": [],
    "recent_decisions": [],
    "status": "monitoring",
    "alert": None,
}
```

## Notes For Monitoring

`dock_history` starts empty when monitoring begins. Each monitor tick should add
the latest observed dock count for the target station.

`recent_decisions` also starts empty. Each monitor tick should add the final
action chosen by the agent, such as `set_next_check`, `alert_user`, or
`finish_trip`.

The monitor loop should use `minutes_to_arrival` to decide urgency:

```text
Low docks + rider arriving soon = alert sooner.
Low docks + rider still far away = check again soon.
Plenty of docks = keep monitoring quietly.
```
