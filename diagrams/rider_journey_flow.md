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

## Color Legend

```mermaid
flowchart LR
    classDef gemini fill:#dbeafe,stroke:#2563eb,color:#111827
    classDef app fill:#dcfce7,stroke:#16a34a,color:#111827
    classDef external fill:#fef3c7,stroke:#d97706,color:#111827
    classDef speech fill:#fee2e2,stroke:#dc2626,color:#111827

    A["Gemini involvement"]
    B["Streamlit or Python app logic"]
    C["External services or feeds"]
    D["Rider facing spoken output"]

    class A gemini
    class B app
    class C external
    class D speech
```

## Overall Journey

```mermaid
flowchart TD
    classDef gemini fill:#dbeafe,stroke:#2563eb,color:#111827
    classDef app fill:#dcfce7,stroke:#16a34a,color:#111827
    classDef external fill:#fef3c7,stroke:#d97706,color:#111827
    classDef speech fill:#fee2e2,stroke:#dc2626,color:#111827

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

    class C gemini
    class B external
    class D,E,H,I,J,K,L,M,O,S,T,U app
    class F,P,Q speech
```

## Monitor Loop Detail

```mermaid
flowchart TD
    classDef gemini fill:#dbeafe,stroke:#2563eb,color:#111827
    classDef app fill:#dcfce7,stroke:#16a34a,color:#111827
    classDef external fill:#fef3c7,stroke:#d97706,color:#111827
    classDef speech fill:#fee2e2,stroke:#dc2626,color:#111827

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

    class U,V gemini
    class B external
    class A,D,E,G,J,L,M,N,O,P,R,T,Y,AA,K app
    class H,S,X speech
```

## Rider Command Loop

```mermaid
flowchart TD
    classDef gemini fill:#dbeafe,stroke:#2563eb,color:#111827
    classDef app fill:#dcfce7,stroke:#16a34a,color:#111827
    classDef external fill:#fef3c7,stroke:#d97706,color:#111827
    classDef speech fill:#fee2e2,stroke:#dc2626,color:#111827

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

    class C gemini
    class B external
    class H,I,L,M app
    class E,F,G,J speech
```

## Gemini Tool Calling Detail

```mermaid
flowchart TD
    classDef gemini fill:#dbeafe,stroke:#2563eb,color:#111827
    classDef app fill:#dcfce7,stroke:#16a34a,color:#111827
    classDef external fill:#fef3c7,stroke:#d97706,color:#111827
    classDef speech fill:#fee2e2,stroke:#dc2626,color:#111827

    %% Request
    A["App Sends Context"] --> B["Gemini Reviews"]
    B --> C{"Tool Needed"}

    %% No tool
    C -->|"no"| D["Return JSON"]

    %% Tool path
    C -->|"yes"| E["Request Tool"]
    E --> F["Python Executes"]
    F --> G["Bike Share API"]
    G --> H["Tool Result"]
    H --> I["Return To Gemini"]
    I --> J["Gemini Decides"]
    J --> K["Return JSON"]
    K --> L["App Applies Result"]
    L --> M["Speak If Needed"]

    class B,C,E,I,J,K gemini
    class A,D,F,H,L app
    class G external
    class M speech
```

## What This Shows

DockTalk has two loops after setup: the monitor loop and the rider command loop. The monitor loop checks live station data and decides whether an update is worth speaking. The rider command loop lets the rider ask for updates, hear options, switch stations, change destination, or stop monitoring.

## What To Notice

The backup station scout runs quietly in the background. The rider hears about backups only when they ask or when the target station risk becomes actionable.

Gemini is not the timer or the data source. Streamlit and Python run the monitor loop, while Gemini helps with parsing, contextual risk, and spoken wording.

For tool calling, Gemini should request tools from the app, but Python should execute them. Gemini should not invent stations or dock counts. It should receive tool results, then return structured JSON that the app can validate and apply.
