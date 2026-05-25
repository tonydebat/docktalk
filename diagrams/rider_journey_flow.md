# DockTalk Rider Journey Flow

## Goal

This diagram shows how the rider journey works from the first voice request through monitoring, risk evaluation, alternative station scouting, and rider commands.

Audience: project team.

Diagram type: flowchart. This is the clearest format for seeing the rider journey and the decision points before coding.

## Key Components

- Rider: speaks requests and confirms choices.
- Streamlit App: owns the UI, session state, and monitor loop.
- Whisper: turns rider speech into text.
- Gemini: parses intent and helps with contextual risk.
- Bike Share API: provides live station data.
- Station Scout: keeps backup stations ready.
- Alert Logic: decides whether to stay quiet or speak.

## Overall Journey

```mermaid
flowchart TD
    %% Setup
    A["Rider Speaks"] --> B["Whisper Transcript"]
    B --> C["Gemini Parses"]
    C --> D["Fetch Stations"]
    D --> E["Rank Stations"]
    E --> F["Speak Options"]
    F --> G{"Rider Confirms"}

    %% Confirmation
    G -->|"yes"| H["Store Target"]
    G -->|"no"| C
    H --> I["Scout Backups"]
    I --> J["Start Monitor"]

    %% Monitoring
    J --> K["Poll Status"]
    K --> L["Refresh Backups"]
    L --> M["Evaluate Risk"]
    M --> N{"Action Needed"}

    %% Quiet path
    N -->|"no"| O["Stay Quiet"]
    O --> K

    %% Alert path
    N -->|"yes"| P["Prepare Message"]
    P --> Q["Speak Update"]
    Q --> R{"Rider Responds"}

    %% Rider response
    R -->|"accept switch"| S["Switch Target"]
    S --> I
    R -->|"ask options"| T["Speak Backups"]
    T --> K
    R -->|"change destination"| C
    R -->|"stop"| U["Stop Monitor"]
    R -->|"ignore"| K
```

## Monitor Loop Detail

```mermaid
flowchart TD
    %% Tick
    A["Due Tick"] --> B["Fetch Live Data"]
    B --> C{"Fetch Works"}

    %% Failure path
    C -->|"no"| D["Keep Last Data"]
    D --> E["Mark Data Stale"]
    E --> F{"Retry Count"}
    F -->|"under limit"| G["Retry Soon"]
    G --> B
    F -->|"over limit"| H["Speak Stale Warning"]
    H --> I{"Stale Too Long"}
    I -->|"no"| J["Retry Next Tick"]
    J --> B
    I -->|"yes"| K["Stop Monitoring"]

    %% Success path
    C -->|"yes"| L["Reset Failures"]
    L --> M["Update Target"]
    M --> N["Refresh Backups"]
    N --> O["Replace Backups"]
    O --> P["Update ETA"]
    P --> Q{"Target Clear"}

    %% Clear cases
    Q -->|"safe"| R["Stay Quiet"]
    Q -->|"full"| S["Recommend Switch"]
    Q -->|"offline"| S
    Q -->|"unclear"| T["Build Context"]

    %% Contextual risk
    T --> U["Call Gemini"]
    U --> V["Risk Decision"]
    V --> W{"Action Needed"}
    W -->|"no"| R
    W -->|"warn"| X["Speak Warning"]
    W -->|"switch"| S

    %% Continue
    R --> Y["Next Tick"]
    X --> Y
    S --> Z{"Rider Choice"}
    Z -->|"switch"| AA["New Target"]
    AA --> N
    Z -->|"stay"| Y
    Z -->|"stop"| K
```

## Rider Command Loop

```mermaid
flowchart TD
    A["Rider Command"] --> B["Whisper Transcript"]
    B --> C["Gemini Command Parser"]
    C --> D{"Command Type"}

    D -->|"get update"| E["Speak Status"]
    D -->|"hear options"| F["Speak Backups"]
    D -->|"switch station"| G["Confirm Switch"]
    D -->|"change destination"| H["Restart Setup"]
    D -->|"stop monitoring"| I["Stop Monitor"]
    D -->|"unknown"| J["Bounded Help"]

    G --> K{"Rider Confirms"}
    K -->|"yes"| L["Set New Target"]
    K -->|"no"| M["Keep Target"]
```

## What This Shows

DockTalk has two loops after setup: the monitor loop and the rider command loop. The monitor loop checks live station data and decides whether an update is worth speaking. The rider command loop lets the rider ask for updates, hear options, switch stations, change destination, or stop monitoring.

## What To Notice

The backup station scout runs quietly in the background. The rider hears about backups only when they ask or when the target station risk becomes actionable.

Gemini is not the timer or the data source. Streamlit and Python run the monitor loop, while Gemini helps with parsing, contextual risk, and spoken wording.
