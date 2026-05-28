# DockTalk Implementation Plan

## Starting Point

The core agent prototypes have been promoted into `src/bikeshare`:

- `predictor.py`: rule-based arrival risk predictor.
- `tools.py`: six-tool Gemini tool catalog and dispatch layer.
- `agent.py`: Gemini monitor loop with deterministic fallback.

The working product is not integrated yet. The immediate goal is a text-first Streamlit demo that proves the full path:

```text
destination text -> station match -> confirm target -> monitor tick -> trace -> alert -> alternatives
```

Voice input and rider commands should come after this path works.

## Phase 1: Stabilize The Package

Goal: make the promoted modules boring to import and easy to test.

Tasks:

- Add `requirements.txt` with the packages already used by the project.
- Add smoke tests for `predictor.py`, `tools.py`, and `agent.py`.
- Keep network tests separate from offline tests.
- Update stale docs that still mention 7 or 9 tools.
- Decide one import style for app code: use `from src.bikeshare...` while running from repo root.

Done when:

```text
python -m py_compile src/bikeshare/*.py
python src/bikeshare/predictor.py
python src/bikeshare/tools.py
```

all work from the repo root.

## Phase 2: Build Text Setup

Goal: type a rider request and get one confirmed target station.

Tasks:

- Implement `src/bikeshare/parsing.py` for a simple text request parser.
- Add a deterministic station matcher first: phrase search over station names.
- Keep Gemini fuzzy matching as a later improvement, not a blocker.
- Return a small station recommendation object with station ID, name, dock count, status, and location hint.
- Add a confirmation step in Streamlit.

Done when:

```text
"City Hall in 10 minutes" -> matched station -> Start monitoring button
```

works in the app.

## Phase 3: Build Streamlit Monitor View

Goal: confirmed station state appears on screen and can run one monitor tick.

Tasks:

- Implement `app/streamlit_app.py` with session state for trip state.
- Show target station, ETA, current docks, and monitor status.
- Add a `Run monitor tick` button before using scheduled fragments.
- Call `src.bikeshare.agent.run_tick()`.
- Render trace entries from the returned result.
- Show whether the tick came from Gemini or fallback.

Done when:

```text
confirmed target -> Run monitor tick -> trace visible -> next_check_seconds visible
```

works without voice.

## Phase 4: Add Demo Controls

Goal: force the full lifecycle in under four minutes.

Tasks:

- Add demo mode toggle.
- Add simulated dock count sequence for the target station.
- Add a reset button for trip state.
- Add compressed timing settings.
- Make the simulated data clearly labeled in the UI.

Done when:

```text
safe docks -> low docks -> alert -> alternatives
```

can be shown reliably without waiting for real Toronto demand.

## Phase 5: Alert And Switch Flow

Goal: the alert panel is useful enough for the demo.

Tasks:

- Render `alert_user` headline, message, and alternatives.
- Add visible buttons to switch to each alternative.
- On switch, update `target_station_id`, clear `alert`, reset dock history, and set `target_just_switched`.
- Track rejected station IDs so Gemini does not recommend the old target immediately.

Done when:

```text
alert appears -> click alternative -> new target is monitored
```

works.

## Phase 6: Voice Polish

Goal: make the demo feel voice-first without making voice a dependency.

Tasks:

- Add `st.audio_input()` for setup voice.
- Wire audio to Whisper transcription.
- Feed transcript into the same text setup path.
- Add browser `speechSynthesis` for spoken confirmation and alerts.
- Keep all messages visible as text.

Done when:

```text
voice setup works, but text setup remains the safe fallback
```

## Phase 7: Demo Hardening

Goal: prepare for submission and live demo.

Tasks:

- Test with Gemini API key present.
- Test with Gemini API key missing or invalid.
- Test live GBFS load.
- Test demo mode reset from a fresh browser session.
- Update README to match the current architecture.
- Record a backup demo video.
- Write the short submission demo plan.

## Immediate Next Step

Build `app/streamlit_app.py` with the text setup path and one manual monitor tick. Do not start with voice or rider commands.
