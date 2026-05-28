# Frontend is a thin relay — no JS framework

The `app/static/` frontend (HTML + JS) does exactly three things: open a WebSocket to FastAPI, stream microphone PCM audio upstream, and play received audio downstream. Visual status updates (target station, dock count, risk state) are rendered by updating a small set of DOM elements directly when JSON events arrive on the same WebSocket. No JS framework (HTMX, Alpine.js, React, etc.) is added.

## Reasoning

The frontend owns no logic — no routing, no state management, no business rules. Every interesting decision happens server-side (Python). Adding a framework would be weight without function. The one UI interaction that requires a tap target (destination confirmation) is a single button; vanilla JS is sufficient.

## Consequences

- If the frontend ever needs meaningful interactivity beyond the v1 scope, this decision should be revisited before accreting vanilla JS workarounds.
- The frontend file count stays small: `index.html`, `client.js`. No build step, no bundler.
