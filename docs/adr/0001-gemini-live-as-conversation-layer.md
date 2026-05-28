# Use Gemini Live API as the conversation layer

DockTalk's voice conversation with the rider (the **conversation loop**) runs on Google's Gemini Live API rather than OpenAI's Realtime API, because the **monitor loop** already uses Gemini Flash for grounded risk evaluation and spoken alert wording — keeping both loops on the same model family avoids re-solving the "do not invent station facts" constraint in a second model, removes a Gemini-writes / OpenAI-speaks semantic seam in monitoring alerts, and lets the conversation tool schemas reuse the same `FunctionDeclaration` shapes already used by the risk evaluator.

## Considered Options

- **OpenAI Realtime API (GPT-4o-realtime)** — more mature function calling and arguably better voice quality today, but introduces a second provider, a second prompt/tool-schema flavor, and a cross-provider hand-off for monitoring alert wording.
- **Gemini Live API** — newer and less battle-tested, but single-provider, single key, single tool schema, single voice on both sides of the Loop A / Loop B seam.

## Consequences

- All Realtime-vs-Live differences in the conversion plan (`docs/convert-to-fastapi-and-openai-realtime-api.md`) collapse to "Live": WebSocket endpoint, event names, tool-call event flow.
- The Realtime system prompt file becomes a Gemini Live system prompt; it can be derived from the existing `docktalk/agent/prompts/system.md` rather than written from scratch.
- DockTalk's production runtime quality is bounded by Gemini Live's voice and function-calling maturity. If Live regresses or proves unfit, reversing this decision means re-writing the conversation-layer shell (not the tools, not the monitor loop).
