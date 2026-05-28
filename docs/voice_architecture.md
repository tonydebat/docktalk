# DockTalk Voice Architecture

## Purpose

This note explains how DockTalk handles voice input and voice output.

Voice is the interface layer. It should make setup and alerts feel natural, but the core monitoring workflow must still work if voice input or audio playback fails.

## Short Version

```text
Rider speech input:
audio -> Whisper -> text -> Gemini or Python

DockTalk speech output:
message text -> Gemini TTS or browser speechSynthesis -> audio

Reliable fallback:
always show the same message as text on screen
```

## Voice Input

Use Whisper for speech-to-text.

Whisper only handles this direction:

```text
spoken audio -> text transcript
```

DockTalk uses this during setup when the rider says something like:

```text
I am heading to Union Station and should be there in about 30 minutes.
```

The app records audio in the browser, sends it to the backend, calls Whisper, and receives a transcript. After that point, the flow is the same as typed input.

Recommended behavior:

```text
voice input -> Whisper transcript -> parse trip -> match station -> ask for confirmation
```

Text input should remain visible as a fallback.

## Voice Output

Whisper does not do text-to-speech.

To speak a DockTalk message out loud, use a text-to-speech layer.

Examples of rider-facing messages:

```text
I found Union Station near Front and Bay. It has four open docks right now. Should I monitor it?
```

```text
Union Station is filling up faster than expected. I recommend switching to Bay and Front, which has seven open docks.
```

## Recommended Output Stack

Use three layers:

```text
1. Show text on screen
2. Try Gemini TTS for better voice quality
3. Fall back to browser speechSynthesis if Gemini TTS fails
```

Screen text is required. Spoken audio is helpful, but it should never block setup, monitoring, or alerts.

## Gemini TTS

Gemini TTS produces better audio than the browser speech engine in local testing.

Use it for the primary demo voice when possible.

Suggested model:

```text
gemini-2.5-flash-preview-tts
```

Suggested voice tested:

```text
Kore
```

The Gemini TTS API returns raw PCM audio. The app or helper script wraps those bytes in a WAV file before playback.

Current smoke test:

```powershell
python scratch\test_gemini_tts.py
```

Expected output:

```text
outputs\gemini_tts_test.wav
```

## Browser Speech Synthesis

Browser speech synthesis is still useful as a fallback.

Pros:

- free
- no API call
- no generated audio file needed
- works directly in the browser

Cons:

- voice quality varies by device and browser
- often sounds less natural
- browser autoplay rules may require a user gesture

Current local browser test:

```text
scratch/test_text_to_speech.html
```

Current Streamlit test:

```powershell
python -m streamlit run scratch\test_text_to_speech_streamlit.py
```

## Streamlit Integration Shape

The app should treat messages as text first.

Suggested helper flow:

```python
message = "I found Union Station near Front and Bay. Should I monitor it?"

show_message(message)
try_play_gemini_tts(message)
fallback_to_browser_speech(message)
```

Do not make the monitor wait for TTS before updating state.

Bad coupling:

```text
agent decision -> wait for audio generation -> update app state
```

Better coupling:

```text
agent decision -> update app state and display text -> attempt audio playback
```

## Confirmation Workflow

When DockTalk finds a likely station:

```text
1. Show station name and location cue.
2. Speak the confirmation message if audio output is available.
3. Offer visible buttons: Start monitoring, Choose another station.
4. Also accept voice confirmation through Whisper.
```

Example:

```text
I found Union Station near Front and Bay. It has four open docks right now. Should I monitor it?
```

The rider can respond with:

```text
Yes, monitor it.
```

or click:

```text
Start monitoring
```

## Alert Workflow

When the monitor decides to alert:

```text
1. Store the alert in session state.
2. Show the alert text and recommended station on screen.
3. Try Gemini TTS.
4. If Gemini TTS fails, optionally use browser speechSynthesis.
5. Continue the monitor loop regardless of audio success.
```

## Demo Rule

For the hackathon demo:

```text
Voice input proves the setup is easy.
Gemini TTS makes the demo feel polished.
Screen text keeps the demo safe.
The autonomous monitor loop is still the core product value.
```
