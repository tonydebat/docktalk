# Voice Input — Destination Selection

## Approach: Record in Browser → Whisper → Resolution Cascade

The rider speaks a destination (address, intersection, or landmark) into the browser microphone. The audio is captured client-side, sent to the Python server, and transcribed by the OpenAI Whisper API. The transcript is then resolved to one or more candidate stations through a three-step cascade:

1. **Gemini** turns the transcript into a small ranked list of search terms; each is tried against the station name index.
2. If that fails, **Nominatim** geocodes the rider's raw words and the nearest stations within 1500 m are returned.
3. If both fail, **Gemini** drives a bounded clarification loop (≤ 2 turns) asking the rider to describe the destination differently.

The rider always sees the transcript (so they can confirm what was heard) and a top-3 selectable candidate list with honest `recommendation_reason` text — including, for geocoded hits, the place name the geocoder understood.

This approach is aligned with the planned stack (Whisper is listed in the project tech table) and is the only one that works reliably across both desktop browsers and iOS Safari.

---

## Why Not the Browser Web Speech API

The browser-native `SpeechRecognition` API is free and requires no server round-trip, but it fails in the DockTalk context:

- Streamlit renders custom components inside iframes; iOS Safari blocks or drops `SpeechRecognition` requests inside iframes.
- iOS Chrome does not support `SpeechRecognition` at all (it uses WebKit, which does not implement the API).
- Transcription quality is inconsistent for Toronto intersection names and landmarks.

Whisper is more reliable, handles proper nouns and accented speech better, and works on every platform that can record audio.

---

## iOS Safari Considerations

iOS Safari uses the native MediaRecorder and outputs **M4A / AAC** audio. Desktop browsers (Chrome, Firefox, Edge) output **WebM / Opus**.

OpenAI Whisper accepts both formats natively (`audio/mp4` and `audio/webm`), so no conversion is needed. The only requirement is to pass the correct file extension alongside the audio bytes when calling the API, so Whisper knows the container format.

The `audio-recorder-streamlit` widget returns **raw bytes only** — it does not surface a MIME type. Container format is therefore inferred server-side from the rider's User-Agent (`st.context.headers`): iPhone/iPad → `.m4a`, everything else → `.webm`. This is sufficient because the widget uses each browser's native `MediaRecorder` default, which is platform-deterministic.

One important iOS-specific rule: the browser microphone permission prompt only fires on a direct user gesture (a tap). The audio recorder widget must be rendered unconditionally on first page load, not inside a conditional block that only appears after a button click, otherwise iOS Safari will silently suppress the permission request.

---

## Full Flow

```
Rider taps mic button
        │
        ▼
Browser captures audio (WebM on desktop, M4A on iOS)
        │
        ▼
audio_recorder() returns raw bytes to Python; container is inferred from User-Agent
        │
        ▼
transcribe_audio(audio_bytes, mime_type)
  → OpenAI Whisper API
  → returns transcript string
  e.g. "I want to return my bike near Union Station"
        │
        ▼
resolve_destination(transcript, all_stations)
  │
  ├── Step 1: parse_destination_intent(transcript)
  │     → Gemini returns ranked candidate terms
  │       e.g. ["Union Station", "Front and Bay", "Front St"]
  │     → try each in search_stations; first hit wins
  │
  ├── Step 2 (only if Step 1 empty):
  │     geocode_to_nearby_stations(transcript, all_stations)
  │     → Nominatim → lat/lon → K nearest stations within 1500 m
  │     → recommendation_reason = "closest dock to {place}"
  │
  └── returns up to 5 candidates, or [] if both steps fail
        │
        ▼
  If candidates returned:
    Streamlit shows transcript + up to 3 candidates as tappable cards;
    audio_recorder stays visible for voice selection.
        │
        ▼
    Rider picks ONE — tap a card, or speak ("first one", "Union Station",
    or a new destination). Gemini classifies voice into:
      • {intent: "select", index: N}     → set target, start monitoring
      • {intent: "new_destination", ...} → re-enter cascade with new transcript
    Tap wins on race; in-flight audio is discarded.
        │
        ▼
    make_initial_trip_state(target_station_id, target_station_name, arrival)
    The two unselected candidates are discarded (they do NOT seed backup_station_ids).
        │
        ▼
    Monitoring starts (same path as today)

  If candidates empty → Step 3: clarification loop (≤ 2 turns)
    clarify_destination(history) → Gemini returns {kind, spoken_question}
    Speak the question via voice.py
    Rider records again → loop back to transcribe_audio
    After 2 failed clarifications → speak terminal message, auto-focus Text tab
```

---

## Resolution Cascade

`resolve_destination` runs a deterministic cascade. Each step is honest about what it does, and the rider never sees a station that the system can't justify.

### Step 1 — Ranked candidate terms

`parse_destination_intent` returns a small ranked `list[str]` of search terms (full name, intersection form, partial street fragment). Each is tried in order against `search_stations`. The first term with hits wins; results are returned and normalised to the standard recommendation object shape (see "Normalisation" below) with `recommendation_reason = "name match for '{term}'"`.

This single richer Gemini call replaces what was previously two prompts, and handles the common "wrong intersection order" and "partial street name" failure modes without an extra round-trip.

### Step 2 — Geocoding fallback

If all ranked terms return zero hits, geocode the **original transcript** (not Gemini's reformulations — preserve the rider's actual words for the geocoder) via Nominatim.

- Provider: Nominatim (OpenStreetMap). No API key. Self-throttled to 1 req/sec. Attribution displayed in the Streamlit footer.
- On a hit, compute the K nearest stations to the returned lat/lon. K=5 internally, top 3 shown to the rider, matching the text path.
- **Radius cap: 1500 m.** Stations beyond this are excluded. If no stations fall inside the cap, the geocode is treated as a miss and the cascade continues to Step 3.
- Apply the same offline/0-dock filter as the text path (consistent with the station recommendation contract).
- Each candidate's `recommendation_reason` is set to `"closest dock to {place}"` where `{place}` is the human-readable name Nominatim returns (`display_name` or its leading component), so the rider can sanity-check that the geocoder understood them.

### Normalisation

Both steps return data through `resolve_destination` in the standard recommendation object shape defined in the station recommendation contract:

```json
{
  "station_id": "...",
  "name": "...",
  "location_hint": "...",
  "available_docks": N,
  "distance_meters": N,
  "station_status": "active",
  "recommendation_reason": "..."
}
```

`resolve_destination` owns this normalisation. `search_stations` and `geocode_to_nearby_stations` may return their raw forms internally; the resolver maps them to the contract shape before returning. The Voice tab's confirmation cards therefore render exactly one shape regardless of which cascade step produced the candidates.

### Step 3 — Bounded clarification loop

If Step 2 also fails, enter a clarification loop:

- Gemini is called with the **full history of failed transcripts** as context and returns structured output:
  ```json
  {
    "kind": "intersection | major_street | spelling | describe_nearby",
    "spoken_question": "..."
  }
  ```
  `kind` is a bounded enum used for state tracking and to avoid asking the same clarification type twice. `spoken_question` is free-text — Gemini composes the wording, which is consistent with the project rule that Gemini owns spoken wording. No station facts are at risk in a clarification question.
- The spoken question is played via the existing TTS (`voice.py`).
- The rider records another response. That transcript is appended to the history. The cascade restarts at Step 1 with the new transcript.
- **Maximum 2 clarification turns** (initial attempt + 2 retries = 3 recordings total). After the second failed clarification, the cascade terminates.

### Step 4 — Terminal failure

If clarification is exhausted, the system speaks a final honest message (*"I can't pin that down. Please type it instead."*), disables further voice recording for this setup session, and auto-focuses the Text tab. No station is ever invented.

### State

The clarification loop's state — `attempt_history: list[str]`, `clarification_turns_remaining: int` — lives in a small dict in `st.session_state` scoped to the Setup expander. It does **not** touch the monitor state machine, which still starts only after the rider confirms a station.

---

## Candidate Confirmation

Once the cascade returns one to three candidates, the rider is asked to pick **exactly one** — the target station. The other candidates are shown only to give the rider context; they are discarded the moment a target is chosen.

### Selection mechanics

The rider can confirm by **tap or voice** (either path is always available):

- **Tap.** Each candidate renders as a clickable card. A tap immediately ends the confirmation phase, sets the target, and starts monitoring. **Tap wins on race**: if a tap arrives while audio is recording, the tap is honoured and the in-flight recording is discarded.
- **Voice.** The same `audio_recorder` widget remains visible. When new audio arrives, Whisper transcribes it and Gemini classifies the intent with structured output:

  ```json
  { "intent": "select", "index": 0 | 1 | 2 }
  ```

  or

  ```json
  { "intent": "new_destination", "transcript": "..." }
  ```

  Gemini receives the transcript **plus** the three candidate names and indices, so it can resolve ordinals ("the first one"), exact names ("Union Station"), partial names ("the Union one"), and mixed phrases. Free-form natural language is supported by design.

### Intent handling

- `select` → set the chosen candidate's `station_id` as `target_station_id`, call `make_initial_trip_state`, exit the voice flow. Monitoring starts.
- `new_destination` → re-enter the resolution cascade with the new transcript. This is the rider voluntarily changing their mind, **not** a system failure, so it does **not** consume a clarification turn.
- Low-confidence / unparseable → speak *"I heard '{transcript}'. Tap one of the three, or try again."* and stay on the same screen. No state change, no turn burned.

### What is carried forward

**Only the rider's selected station becomes the target.** The two unselected candidates are discarded. They do **not** seed `backup_station_ids` in the monitor state.

Rationale: the voice candidates are ranked by similarity to what the rider said (name match, or proximity to a geocoded place). The monitor's backup tracking is ranked by proximity to the chosen target and live dock availability — a different signal. The monitor owns its own backup-selection logic and computes `backup_station_ids` fresh once monitoring starts; voice has no part in that.

---

## Failure Handling

The cascade does up to three serial network calls per attempt (Whisper → Gemini → Nominatim), times up to three rider attempts. Each step has a graceful degradation path so that one provider's outage never crashes the setup flow.

**Guiding principle:** distinguish *system* failures from *rider* failures. Network errors retry the same step or fall through to the next one; they never burn a clarification turn. Only "rider said something we couldn't resolve" decrements the clarification counter.

### Per-step behaviour

| Failure | Behaviour |
|---|---|
| Whisper timeout / 5xx | Speak *"I couldn't hear you, please try again."* Stay on the Voice tab. **Do not** decrement `clarification_turns_remaining`. |
| Whisper returns an empty transcript | Same as above — treat as a re-record prompt, no counter decrement. |
| Gemini fails on `parse_destination_intent` | Step 1 returns `[]`. The cascade falls through to Step 2 (geocode the raw transcript). The rider's actual words may still geocode. |
| Nominatim timeout / error / no result / no stations within 1500 m | `geocode_to_nearby_stations` returns `[]`. The cascade falls through to Step 3 (clarification). |
| Gemini fails on `clarify_destination` | Speak a hardcoded fallback question (*"Can you describe a nearby intersection?"*) and continue the loop. Do not blow up. |

### Timeouts

- Whisper: 30 s (audio uploads dominate)
- Gemini: 10 s per call
- Nominatim: 5 s (also the most failure-prone given the 1 req/sec public-policy limit)

### Startup-time checks

Missing API keys are checked when the Streamlit app boots, not at first voice attempt:

- If `OPENAI_API_KEY` or `GEMINI_API_KEY` is unset, the Voice tab is rendered but **disabled** with a visible banner: *"Voice input is unavailable: missing API key."* The Text tab keeps working.
- This avoids the worst failure mode — a rider taps the mic, grants permission, speaks, then sees a stack trace because a secret was missing.

---

## Dependencies

| Package | Purpose |
|---|---|
| `audio-recorder-streamlit` | In-browser microphone capture; returns raw audio bytes (no MIME type — container is inferred from User-Agent) |
| `openai>=1.0` | Whisper API client (`client.audio.transcriptions.create`) |
| `google-genai` | Already present; used for destination intent parsing and clarification |
| `geopy` | Nominatim client for geocoding fallback (Step 2 of the resolution cascade) |

Add to `pyproject.toml`:

```toml
"audio-recorder-streamlit>=0.0.10",
"openai>=1.0",
"geopy>=2.4",
```

---

## New Modules

### `src/bikeshare/transcription.py`

(Named to avoid collision with the existing `voice.py`, which builds the
browser TTS snippet. The two modules don't share a domain — one renders
JS for `SpeechSynthesisUtterance`, the other calls Whisper — so a shared
"voice" prefix would overstate the relationship.)

Two functions:

- **`transcribe_audio(audio_bytes: bytes, mime_type: str) -> str`**
  Calls the Whisper API (`whisper-1` model). Sets the file tuple extension based on `mime_type` (`audio/mp4` → `.m4a`, `audio/webm` → `.webm`). Returns the raw transcript string.

- **`infer_audio_mime_from_headers(headers: Mapping[str, str]) -> str`**
  Returns `"audio/mp4"` for iPhone/iPad User-Agents, `"audio/webm"` otherwise. Accepts the headers mapping (e.g. `st.context.headers`) rather than reading Streamlit context internally — `transcription.py` stays Streamlit-free and the rule is unit-testable in isolation. Handles known edge cases (iPadOS 13+ identifying as Mac, missing/empty User-Agent → defaults to webm).

### `src/bikeshare/parsing.py`

(Reuses the existing empty stub. This is where "Gemini turns free text
into a structured value" lives.)

Three functions:

- **`parse_destination_intent(transcript: str) -> list[str]`**
  Sends the transcript to Gemini with a system prompt asking for a small ranked list of search terms (full name, intersection form, street fragment). Returns the list in priority order (e.g. `["Union Station", "Front and Bay", "Front St"]`).

- **`clarify_destination(history: list[str]) -> ClarificationPrompt`**
  Called when both the ranked-terms search and the geocoding fallback fail. Sends the full history of failed transcripts to Gemini, which returns structured output `{kind, spoken_question}` where `kind` is one of `intersection | major_street | spelling | describe_nearby`. The `kind` is tracked in session state so the same clarification type isn't repeated; the `spoken_question` is free-text wording chosen by Gemini.

- **`classify_selection_intent(transcript: str, candidates: list[dict]) -> SelectionIntent`**
  Called during the confirmation phase when the rider speaks. Gemini receives the transcript plus the candidate names and indices and returns structured output of one of two shapes: `{"intent": "select", "index": int}` (rider chose one of the displayed candidates by ordinal or name) or `{"intent": "new_destination", "transcript": str}` (rider voluntarily changed their mind — the cascade re-enters with the new transcript, no clarification turn consumed). Low-confidence parses return `{"intent": "unclear"}`.

### `src/bikeshare/geocoding.py`

(Reuses the existing empty stub.)

One function:

- **`geocode_to_nearby_stations(transcript: str, all_stations: dict, *, radius_m: int = 1500, k: int = 5) -> list[dict]`**
  Calls Nominatim (via `geopy.Nominatim`) on the raw transcript. If a result is returned, finds the K nearest stations within `radius_m` and returns them in standard recommendation object shape with `recommendation_reason = "closest dock to {place}"`. Filters out offline/0-dock stations. Returns `[]` if Nominatim misses, if no stations fall within the cap, or on any Nominatim error.

### `src/bikeshare/destination_resolver.py`

One function:

- **`resolve_destination(transcript: str, all_stations: dict) -> list[dict]`**
  Orchestrates the resolution cascade described above. Takes the raw transcript (not a pre-parsed query) so that Step 2 can geocode the rider's actual words. Returns up to 5 ranked station dicts in the standard recommendation object shape. Returns `[]` only when both Step 1 (ranked terms) and Step 2 (geocoding) fail; the caller (Streamlit UI) is then responsible for invoking `clarify_destination` and looping.

---

## Streamlit UI Changes (`app/streamlit_app.py`)

Inside the `⚙️ Setup` expander, add a **Voice** tab as the **default (left-most) tab**, above the existing text search:

```
[ 🎙️ Speak destination ]  [ ⌨️ Type destination ]
```

**iOS Safari placement rules (load-bearing, not cosmetic):**

- The Setup expander must be open on first load. The existing `expanded=not monitoring_started` argument already satisfies this on a fresh session.
- Voice must be the **default** tab. Streamlit's `st.tabs` renders all tab contents into the DOM but hides inactive ones with `display: none`; iOS Safari has dropped `MediaRecorder` permission requests inside hidden containers. Defaulting to Voice puts the recorder in the visible, active tab at first paint, which is functionally equivalent to rendering it unconditionally.
- The rider can switch to the Text tab after granting (or denying) microphone permission without issue.

**Voice tab:**
1. Render `audio_recorder()` unconditionally (iOS permission requirement).
2. When bytes are returned, call `transcribe_audio(audio_bytes, mime_type)` then `resolve_destination(transcript, all_stations)`. (Step-1 ranked-term parsing and Step-2 geocoding both live inside `resolve_destination`.)
3. Display the transcript so the rider can verify what was heard.
4. If candidates returned: enter the **confirmation phase** — show up to 3 selectable cards **and** keep the `audio_recorder` visible. The rider may tap a card or speak ("first one", "Union Station", "actually I meant Spadina"). See *Candidate Confirmation* above for intent dispatch.
   - On `select` → `make_initial_trip_state(target_station_id, ..., arrival)` and start monitoring. The two unselected candidates are discarded.
   - On `new_destination` → re-enter the cascade with the new transcript; clarification counter is **not** decremented.
   - On low-confidence intent → stay on screen, prompt the rider to tap or try again.
   - On tap → win the race against any in-flight recording; discard the audio buffer.
5. If candidates empty and `clarification_turns_remaining > 0`: call `clarify_destination(history)`, speak the question via `voice.py`, decrement the counter, append the transcript to `attempt_history`, and re-render the recorder.
6. If clarification exhausted: speak the terminal message, disable the recorder for this setup session, and auto-focus the Text tab.

Voice-tab session state (scoped to the Setup expander; not part of the monitor state machine):

```python
st.session_state.voice_setup = {
    "attempt_history": [],            # list[str] of failed transcripts
    "clarification_turns_remaining": 2,
}
```

**Text tab:**
The existing `st.text_input` + `search_stations` + `st.selectbox` flow, unchanged. Both tabs converge at the same station confirmation and monitoring start step.

---

## API Key

Whisper requires an `OPENAI_API_KEY` in the `.env` file in addition to the existing `GEMINI_API_KEY`.

```
GEMINI_API_KEY=...
OPENAI_API_KEY=...
```

Two providers were chosen deliberately over a Gemini-only path (Gemini 2.0 Flash can ingest audio directly). Whisper has stronger transcription quality on Toronto proper nouns and intersection names, which is the only place in the rider journey where misrecognition would derail setup. The cost is one extra env var and one extra serial network call at session start — both acceptable given that destination capture happens once, not in the monitoring hot path.

