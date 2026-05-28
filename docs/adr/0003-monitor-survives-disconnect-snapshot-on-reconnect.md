# Monitor loop survives WebSocket disconnects; rider gets a snapshot on reconnect

When the browser WebSocket closes unexpectedly, the per-session `asyncio` monitor task keeps running. No individual alerts are queued. On reconnect, the FastAPI WebSocket handler immediately calls `run_tick()` once to produce a current-state snapshot for the rider, rather than replaying what may have happened during the gap.

## Considered Options

- **Stop on close** — cancel the monitor task when the WebSocket closes. Simple, but the rider loses monitoring context and must restart or call a resume endpoint. Rejected because a 10–30 s signal drop is a normal bike-ride event, not a session end.
- **Queue individual alerts, flush on reconnect** — task survives; each fired alert is stored and replayed on reconnect. Rejected because a burst of queued alerts is confusing ("you had 3 alerts while disconnected") and the ordering may no longer be relevant.

## Consequences

- The monitor task must tolerate a period with no live WebSocket. Alert delivery must be conditional on a connected socket.
- The `max_stale_minutes=10` timeout (from `docs/monitoring_spec.md`) still applies: if GBFS data is unreachable for 10 minutes, the task stops regardless of connection state.
- Session cleanup (task cancellation, state removal) must be triggered explicitly — either by the rider calling `POST /session/{id}/stop` or by the stale-data timeout, not by WebSocket close.
