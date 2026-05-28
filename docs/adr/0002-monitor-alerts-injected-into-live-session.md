# Monitor loop alerts are injected into the live Gemini session as verbatim-speak turns

When the monitor loop decides the rider needs to be notified (target station filling up, switch recommended, etc.), it injects the `spoken_message` string — already produced by the risk evaluator — directly into the active Gemini Live session as a client-content turn, with the system prompt instructing the model to speak it verbatim.

## Considered Options

- **Side-channel TTS** — synthesize the alert independently and play it via the browser `<audio>` element, bypassing the live session. Rejected because the conversation-layer model would have no context of the alert, breaking follow-up questions ("wait, which station?").
- **Hybrid context update** — push the risk result as a context message and let the model decide when to speak. Rejected because cooldown and gating are already handled in Python by the monitor loop; adding model discretion introduces an unpredictable timing layer with no clear benefit.

## Consequences

- The system prompt must instruct the model to speak injected monitor alerts verbatim, without rephrasing. If Live paraphrases or adds filler, it re-opens the fact-invention door.
- The `spoken_message` from the risk evaluator is the grounding source of truth; the conversation layer only relays it.
