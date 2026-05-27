# PRD — Voice Input for Destination Selection

Status: ready-for-agent
Related design doc: [`docs/voice_input.md`](../voice_input.md)

## Problem Statement

Today a DockTalk rider has to type their drop-off destination into a Streamlit text input before monitoring can start. On a phone, at the start of a ride, this is awkward — the rider is about to grab a bike, may be wearing gloves, and often only knows the destination as a spoken landmark ("Union Station", "Bay and King") rather than something they can type into a station-name search box.

The project's tagline is *quiet monitoring, actionable alerts*: hands-free interaction is the whole point. Forcing typing at setup undercuts that promise on the most error-prone step of the journey — picking the right target station.

## Solution

Add a **Voice** tab to the Setup expander that lets the rider speak their destination instead of typing it. Audio is captured in the browser, sent to OpenAI Whisper for transcription, and resolved to a small list of candidate stations through a deterministic three-step cascade:

1. Gemini parses the transcript into a ranked list of search terms; each is tried against the station name index.
2. If no name match, Nominatim geocodes the rider's raw words and the K nearest stations within 1500 m are returned.
3. If both fail, Gemini drives a bounded clarification loop (≤ 2 turns) asking the rider to describe the destination differently.

The rider sees the transcript (so they can verify what was heard) and up to three candidate stations. They confirm one station — by tap or by voice — and only that single station becomes the monitoring target. The two unselected candidates are discarded.

A graceful terminal failure ("I can't pin that down. Please type it instead.") falls back to the existing Text tab if voice can't resolve a destination after the clarification budget is exhausted.

## User Stories

1. As a rider, I want to speak my drop-off destination instead of typing it, so that I can set up monitoring with one hand while I'm at the bike rack.
2. As an iPhone rider, I want the mic permission prompt to fire on my first tap, so that I don't have to fight Safari's silent-suppression behaviour.
3. As an Android Chrome rider, I want voice input to work without installing anything, so that I can use DockTalk on any device with a browser.
4. As a rider, I want to see the transcript of what Whisper heard, so that I can spot misrecognition (e.g. "Spadeena" for "Spadina") before I commit to a station.
5. As a rider who said "Union Station", I want to see one or more candidate stations whose names match, so that I can confirm the obvious case in a single tap.
6. As a rider who said "the CN Tower", I want the system to geocode the landmark and show nearby stations, so that I don't get stuck just because the landmark isn't a station name.
7. As a rider, I want the candidate cards to tell me *why* each station was chosen ("name match for 'Union Station'" or "closest dock to CN Tower"), so that I can trust the recommendation.
8. As a rider, I want to tap one of the candidate cards to choose it, so that I can confirm with a single gesture.
9. As a rider whose hands are busy, I want to say "first one" or "Union Station", so that I can confirm without touching the screen.
10. As a rider who changed their mind, I want to say a different destination during confirmation ("actually, Spadina") and have the system re-run the search, so that I don't waste a clarification attempt on a voluntary change of mind.
11. As a rider whose first attempt produced no candidates, I want Gemini to ask me a focused clarifying question (intersection? major street? spell it?), so that I get a useful prompt instead of "try again".
12. As a rider, I want the clarification loop to give up after two failed attempts, so that I don't get trapped in a loop and can fall back to typing.
13. As a rider on a flaky network, I want Whisper timeouts and Nominatim outages to be retried or fall through gracefully, so that one provider's failure doesn't crash the setup flow.
14. As a rider, I want network failures *not* to count against my clarification attempts, so that "system was slow" doesn't get confused with "I said something unclear".
15. As a rider who said "Markham" by mistake, I want the geocoder to refuse to recommend a station 20 km away, so that I don't end up biking toward a useless target.
16. As a rider, I want the Voice tab to be the default tab in the Setup expander, so that the mic permission prompt fires reliably on iOS Safari at first paint.
17. As a rider, I want the Text tab to remain fully functional, so that I can fall back to typing whenever I prefer.
18. As a rider whose voice setup fails completely, I want the Text tab to auto-focus, so that I don't have to hunt for the fallback.
19. As an operator, I want missing `OPENAI_API_KEY` or `GEMINI_API_KEY` to be detected at app startup, so that the Voice tab is disabled with a clear banner instead of crashing on first use.
20. As a rider, I want only the station I selected to be carried forward as the monitoring target, so that the monitor's backup logic stays consistent with what it would compute for the text path.
21. As a rider, I want the cascade to preserve my actual words for geocoding (not Gemini's reformulation), so that a landmark I named survives even if Gemini's name parsing misfires.
22. As a maintainer, I want the resolution cascade to be testable without making real API calls to Whisper, Gemini, or Nominatim, so that the build stays deterministic and fast.
23. As a maintainer, I want the voice modules to follow the project's "Gemini doesn't invent station facts" rule, so that the new feature inherits the project's safety guarantees.
24. As a maintainer, I want the User-Agent → MIME inference logic to live in pure Python with unit tests, so that the iOS/desktop split doesn't quietly break when a browser changes its UA string.
25. As a contributor, I want Nominatim attribution shown in the Streamlit footer, so that we comply with the OSM data licence.

## Implementation Decisions

### Cross-cutting

- **Two-provider voice pipeline** — Whisper for transcription, Gemini for all language work (intent parsing, clarification, selection classification). Trade-off was discussed: Gemini-only could ingest audio directly with one API key, but Whisper's transcription quality on Toronto proper nouns is meaningfully better, and destination capture is a one-time setup step (not a hot path), so the extra env var and round-trip are acceptable. Rationale is captured inline in `docs/voice_input.md`; no separate ADR.
- **No Web Speech API** — iOS Safari blocks `SpeechRecognition` inside Streamlit iframes; iOS Chrome doesn't implement it at all. Whisper is the only thing that works cross-platform.

### Modules

1. **`transcription.py`** (new). Two functions:
   - `transcribe_audio(audio_bytes, mime_type) -> str` — wraps `whisper-1`. Sets the file tuple extension from MIME (`audio/mp4` → `.m4a`, `audio/webm` → `.webm`).
   - `infer_audio_mime_from_headers(headers) -> str` — accepts a headers mapping (e.g. `st.context.headers`). Returns `"audio/mp4"` for iPhone/iPad UAs, `"audio/webm"` otherwise. Handles iPadOS 13+ identifying as Mac and missing-UA edge cases. Pure, no Streamlit dependency.

2. **`parsing.py`** (fills the existing empty stub). Three functions, all Gemini-backed structured-output calls:
   - `parse_destination_intent(transcript) -> list[str]` — returns a ranked list of search terms (full name, intersection form, partial street fragment).
   - `clarify_destination(history: list[str]) -> ClarificationPrompt` — returns `{kind: "intersection"|"major_street"|"spelling"|"describe_nearby", spoken_question: str}`. `kind` is tracked so the same clarification type isn't repeated; `spoken_question` is free-text (Gemini owns spoken wording, consistent with the project rule).
   - `classify_selection_intent(transcript, candidates) -> SelectionIntent` — receives the transcript plus the candidates' names and indices. Returns one of `{intent: "select", index: int}`, `{intent: "new_destination", transcript: str}`, or `{intent: "unclear"}`.

3. **`geocoding.py`** (fills the existing empty stub). One function:
   - `geocode_to_nearby_stations(transcript, all_stations, *, radius_m=1500, k=5) -> list[dict]` — Nominatim via `geopy`. Computes K nearest stations within `radius_m` of the geocoded location, filtered for offline/0-dock stations. Each result is shaped to the recommendation-object contract with `recommendation_reason = "closest dock to {place}"` where `{place}` is the Nominatim display name. Self-throttled to 1 req/sec. Returns `[]` on miss, timeout, error, or no-stations-in-radius.

4. **`destination_resolver.py`** (new). One function:
   - `resolve_destination(transcript, all_stations) -> list[dict]` — orchestrates the cascade. Step 1 (`parse_destination_intent` + `search_stations`), then Step 2 (`geocode_to_nearby_stations`) on miss. Normalises results from both steps to the recommendation-object shape (with `recommendation_reason` derived from the step that produced the candidate). Returns up to 5 candidates ranked, or `[]` if both steps fail; the Streamlit layer handles Step 3 (clarification loop).

5. **`app/streamlit_app.py`** (modified). Voice tab added as the **default (left-most)** tab in the Setup expander. Renders `audio_recorder()` unconditionally so iOS Safari fires the permission prompt on first tap. Drives the cascade, the clarification loop, the confirmation phase, and the terminal-failure fallback. Hosts a small voice-setup state dict in `st.session_state` (scoped to the Setup expander; not part of the monitor state machine):
   ```python
   st.session_state.voice_setup = {
       "attempt_history": [],
       "clarification_turns_remaining": 2,
   }
   ```

6. **`pyproject.toml`** (modified). Adds `audio-recorder-streamlit>=0.0.10`, `openai>=1.0`, `geopy>=2.4`.

### Resolution cascade contract

- **Inputs to `resolve_destination`**: raw transcript + the full station dict from `fetch_all_stations()`.
- **Step 1**: ranked candidate terms from Gemini; each tried in order against `search_stations`; first hit wins. `recommendation_reason = "name match for '{term}'"`.
- **Step 2**: Nominatim geocode of the **original transcript** (not Gemini's reformulation). Up to K=5 nearest stations within 1500 m; offline/0-dock filtered; top 3 shown.
- **Step 3** (Streamlit, not in the resolver): `clarify_destination` → speak via `voice.py` → re-record → re-enter cascade. Bounded at 2 clarification turns total.
- **Step 4**: terminal-failure message, voice recorder disabled for this setup session, Text tab auto-focused.

### Candidate confirmation

- The rider picks **exactly one** target from up to three candidate cards.
- Confirmation accepts **tap OR voice** at the same time. The `audio_recorder` remains visible during confirmation.
- On voice input during confirmation: `classify_selection_intent` decides between `select` (pick this candidate), `new_destination` (re-enter the cascade — does NOT consume a clarification turn), or `unclear` (stay on screen, prompt to tap or try again).
- **Tap wins on race**: a tap during an in-flight recording is honoured; the recording is discarded.
- **Only the chosen station is carried forward.** The other two candidates are discarded. They do NOT seed `backup_station_ids`; the monitor computes backups independently using its own proximity + live-dock-availability signal, which is different from voice's "similarity to transcript" signal.

### Failure handling

| Failure | Behaviour |
|---|---|
| Whisper timeout / 5xx | "I couldn't hear you, please try again." Counter NOT decremented. |
| Whisper returns empty transcript | Same as above. |
| Gemini `parse_destination_intent` fails | Step 1 returns `[]`; cascade falls through to Step 2 on the raw transcript. |
| Nominatim timeout / error / no result / no stations in 1500 m | `geocode_to_nearby_stations` returns `[]`; cascade falls through to Step 3. |
| Gemini `clarify_destination` fails | Speak a hardcoded fallback question ("Can you describe a nearby intersection?") and continue the loop. |
| Missing `OPENAI_API_KEY` or `GEMINI_API_KEY` at startup | Voice tab disabled with a banner. Text tab keeps working. |

Timeouts: Whisper 30 s, Gemini 10 s/call, Nominatim 5 s.

**Guiding principle**: distinguish *system* failures from *rider* failures. Network errors retry the same step or fall through; only "rider said something unresolvable" decrements the clarification counter.

### iOS Safari constraints (load-bearing)

- The Setup expander must be open on first load — the existing `expanded=not monitoring_started` already satisfies this on a fresh session.
- Voice must be the **default tab**. `st.tabs` keeps inactive tabs in the DOM under `display: none`; iOS has historically dropped `MediaRecorder` permission requests inside hidden containers.
- The `audio_recorder` widget must render unconditionally, not behind a button click.
- MIME container is inferred from User-Agent (`audio-recorder-streamlit` returns bytes only); iOS → `.m4a`, else → `.webm`.

## Testing Decisions

**What makes a good test in this feature**: external behaviour only. Pass canned LLM/HTTP responses into the modules through their narrow public interfaces and assert on the returned dict/list shapes and field values. Do NOT assert on prompts, on which model was selected, on internal call counts, or on intermediate state. The Gemini/Whisper/Nominatim clients are replaced with simple fakes — no `unittest.mock` patching of deep internals.

**Prior art in the repo**: `tests/test_station_search.py` and `tests/test_trip_state.py` follow this style — small fixture dicts, public-API calls, assertions on return values, no mocking framework. Match that style.

**Coverage**:

- **`transcription.py`**
  - `infer_audio_mime_from_headers` — full unit coverage: iPhone UA, iPad UA, iPadOS-as-Mac UA, desktop Chrome, desktop Firefox, missing User-Agent key, empty User-Agent string.
  - `transcribe_audio` — skip. It's a one-line wrapper over the OpenAI SDK; testing it tests the SDK.

- **`parsing.py`**
  - All three functions tested against a fake Gemini client returning canned structured output (one fixture per function).
  - `parse_destination_intent`: returns a non-empty `list[str]`; rejects empty transcript.
  - `clarify_destination`: returns `kind` from the bounded enum; `spoken_question` is non-empty; history is preserved when passed.
  - `classify_selection_intent`: each intent shape returns correctly; out-of-range index treated as `unclear`; unknown intent string falls through to `unclear`.

- **`geocoding.py`**
  - `geocode_to_nearby_stations` with a fake Nominatim returning:
    - hit inside radius → list of stations in recommendation-object shape with the right `recommendation_reason`
    - hit outside radius (e.g. Markham) → `[]`
    - geocoder miss → `[]`
    - geocoder error → `[]`
    - hit inside radius but all nearby stations offline/0-docks → `[]`
  - Verify the `radius_m` and `k` parameters are honoured.

- **`destination_resolver.py`**
  - With all three deep modules replaced by stubs, drive the cascade through each path:
    - Step 1 hit (first term matches) → returns step-1 candidates with `"name match for"` reasons
    - Step 1 hit (third term matches) → returns step-1 candidates
    - Step 1 miss + Step 2 hit → returns step-2 candidates with `"closest dock to"` reasons
    - Step 1 miss + Step 2 miss → returns `[]`
  - Verify the **raw transcript** (not the parsed query) is what gets passed to Step 2.
  - Verify the recommendation-object shape is the same regardless of which step produced the candidate.

**Out of test scope**:

- Streamlit UI behaviour (tabs, recording, confirmation cards). This is integration-shaped and brittle; rely on manual testing on iOS Safari + desktop Chrome before each release.
- The actual Whisper/Gemini/Nominatim network calls. The deep modules' interfaces are the unit-test seam.

## Out of Scope

- Voice-driven **rider commands** during monitoring (e.g. "what are my options?"). That's a separate feature in `docs/rider_journey.md`.
- Speech output / text-to-speech for non-voice paths. `voice.py` already handles TTS via the browser; this PRD reuses it but does not change it.
- Capturing the rider's **ETA** by voice. ETA stays hardcoded at `now + 10 minutes` (existing behaviour).
- Geocoding for the **text path**. Only the voice path uses Nominatim; the text path stays pure substring search.
- Backup-station selection logic. The monitor owns backups; voice only chooses the target.
- A persistent alias map for landmarks ("CN Tower" → known intersection). Step 2's geocode covers this case for now.
- Multi-language transcription. English only.

## Further Notes

- **Attribution**: Nominatim usage requires "© OpenStreetMap contributors" displayed somewhere visible. Add it to the Streamlit footer.
- **Self-throttling**: Nominatim's public policy is 1 request per second per application. Implement a simple in-process throttle in `geocoding.py` (e.g., a module-level last-call timestamp).
- **Provider selection rationale**: documented inline in `docs/voice_input.md` under the "API Key" section. No separate ADR — the choice is reversible (swap inside `transcription.py` without changing call-site contracts).
- **Test seams as design contracts**: the deep modules are deep specifically so the test boundary matches the network boundary. Don't add a layer of indirection inside `parsing.py` (e.g., a "GeminiClient" abstraction) — the public function itself is the seam, and the fake is injected via constructor parameter or module-level monkeypatch matching the existing test style.
