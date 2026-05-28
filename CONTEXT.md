# DockTalk

DockTalk is a voice assistant for Bike Share Toronto riders. The rider speaks a return destination; DockTalk recommends a dock station, monitors dock availability while the rider is en route, and speaks only when the rider may need to change plans.

## Language

**Conversation loop**:
The live audio session between the rider and DockTalk. Runs on Gemini Live API over a single persistent WebSocket. Owns intent classification, tool routing, and spoken output to the rider.
_Avoid_: voice loop, chat loop, dialogue loop.

**Monitor loop**:
The background polling task that fetches GBFS station data every 60 s, calls `run_tick()` for risk evaluation, and emits a spoken alert into the conversation loop only when a meaningful change has occurred. Independent of whether the rider is currently speaking.
_Avoid_: poll loop (too narrow — it does more than poll), background loop, watcher.

**Risk evaluator**:
The Gemini Flash call inside the monitor loop that decides whether the rider needs to be told something, and produces the `spoken_message` wording when they do. Distinct from the conversation-layer model (Gemini Live), even though both are Gemini.
_Avoid_: risk model, alert generator.

**Tool**:
A Python function the conversation-layer model can request during a session (e.g. `resolve_destination`, `get_station_status`). The model requests; Python executes; the model speaks the result. Tools are the only path by which station facts enter the rider's ears.
_Avoid_: function, handler, action — reserve those for general Python usage.

**Trip state** (a.k.a. `MonitorState`):
The per-session record of where the rider is going, which station is the current target, which are backups, what risk level the monitor last computed, and when the last spoken alert was emitted. Held in the FastAPI process keyed by session ID.
_Avoid_: session state (too generic), ride state.

**Target station**:
The dock station the rider is currently riding toward. Exactly one at a time.
_Avoid_: destination station (rider's destination is the place, not the dock), chosen station, primary station.

**Backup station**:
A candidate dock station tracked internally as a fallback for the target. Up to 5 tracked; at most 3 spoken when the rider asks for options; exactly 1 recommended on a switch.
_Avoid_: alternative, fallback, secondary station.

**Location hint**:
A short, factually grounded phrase describing where a station is (intersection from official station name, official address, curated alias, or honest weaker phrase like "near Union Station"). Never invented by a model.
_Avoid_: address, intersection — these are sources for the hint, not the hint itself.

## Example dialogue

> **Dev**: When the monitor loop decides the target is no longer viable, who picks the new station?
>
> **Domain**: Python does. The risk evaluator returns a risk level and reasoning, but the actual ranking of backups — distance, dock count, offline filter — is Python in `ranking.py`. The conversation loop only ever sees a bounded list of recommendation objects.
>
> **Dev**: So when the rider says "what are my options," that's a tool call?
>
> **Domain**: Right. The conversation loop calls `get_backup_options`, Python returns up to three backup stations as recommendation objects, and the model speaks them in the multi-option format. It can't pick a fourth or invent an intersection.
