## Why

The current display loop iterates through messages in arrival order with no notion of how recently each was shown, so a message displayed five minutes ago is just as likely to be picked next as one that has been off-screen for a week. This makes the rotation feel repetitive and lets older messages starve. We want the next message to be chosen by a weighted priority that combines how long since it was last displayed, how recently it was sent, and whether the sender flagged it as a favorite — so a never-shown recent message wins, a recently-shown message sits out for a bit, and favorites surface more often. Newly arriving SMS still pre-empt the currently rendering message and display immediately.

Critically: only the renderer (the Pi) actually knows when a message was shown. The Twilio webhook and Flask server are downstream of "an SMS arrived" — they have no signal for "this message is currently being scrolled on the wall." So the source of truth for display-recency lives on the Pi, as an append-only event log on disk. The server's `Message` model is not mutated. The log uses a generic `event_type` schema so future pattern types (`image_display`, `video_display`) plug in cleanly and the log can later be published over MQTT for remote debugging.

## What Changes

- Add a Pi-local append-only event log on disk (`heart-matrix-controller/data/events.jsonl` by default, path configurable). Each render event is one JSONL line: `{event_type, message_id, timestamp, received_at}`. The renderer writes the event immediately after each message advances. The log carries only immutable facts about the event itself — the message's favorite flag (a current-state property) is read from the message record at pick time, not from the log.
- Replace the current first-in/first-out rotation with a deterministic priority-weighted selection function. The selector reads the event log to compute the recency-of-display weight (1.0 = never shown in this pattern, decaying toward 0 as time since last show grows), combines it with recency-of-send (1.0 = newest eligible, 0.0 = oldest eligible) and a favorite boost, and picks the highest-scoring message.
- Apply a configurable eligibility window (default two weeks) — only messages whose `received_at` is within `now − offset` are eligible for the rotation pool. Messages older than the offset are dormant until something else changes.
- Add a `favorite` flag per message (boolean) so the selector can tilt the score toward senders' favorites without mutating the message body. Where `favorite` lives in storage is an implementer choice (either on the `Message` itself or in a separate config-side favorites list, similar to filters) — spec only requires the selector to read the current-state value at pick time.
- Newly arriving messages still bypass the selection function and pre-empt the currently rendering message immediately (this is the only behavioral invariant that does not change). The renderer MAY also write a `preempted` event to the log for debug visibility — out of scope to require, in scope to permit.
- The server's `Message` dataclass and storage schemas are NOT changed by this proposal. The event log is a new artifact that lives only on the Pi. The browser preview maintains its own event log (in IndexedDB) so the preview is illustrative but consistent with itself.
- The selector's three weights (`W_DISPLAY`, `W_SEND`, `W_FAVORITE`), the display-recency decay window (`SATURATION_SECONDS`), the eligibility window (`OFFSET_SECONDS`), and the rollout flag (`USE_WEIGHTED_SELECTOR`) live as module-level constants in `lib_shared/selector.py`. Operators tune them by editing the source and redeploying; they are NOT exposed on the Flask Settings page and are NOT keys in `settings.toml`. Only `EVENT_LOG_PATH` and `EVENT_LOG_MAX_ENTRIES` flow through `settings.toml` because they describe *where the artifact lives on disk*, not *how the algorithm scores*.
- Forward-compatibility: the event schema is generic so a future change can publish events to MQTT for remote debugging, mirror the Pi's log to the server, and subscribe the browser preview to that mirror. Those are explicitly out of scope here; this proposal only ships the Pi-local log + the selector that reads it.

## Capabilities

### New Capabilities

- `message-selection`: defines the priority-weighted algorithm that picks the next message to display, sourcing display-recency from the Pi-local event log and combining it with recency-of-send and favorite boost. Includes the two-week eligibility window and the pre-emption invariant for new arrivals.

### Modified Capabilities

<!-- No existing capability's REQUIREMENTS are changing. MQTT envelope shape, Twilio webhook, Flask storage, and the Pi MQTT subscriber all stay the same; only the picking step downstream of envelope receipt changes, and only on the Pi. -->

## Impact

- `lib_shared/selector.py` (new) — the `MessageSelector` class with `pick(messages, now, event_log) -> Optional[Message]`. Lives in `lib_shared/` so the same Python is reused in the browser preview via PyScript. The selector's tuning knobs are defined as module-level constants at the top of this file (`W_DISPLAY`, `W_SEND`, `W_FAVORITE`, `SATURATION_SECONDS`, `OFFSET_SECONDS`, `USE_WEIGHTED_SELECTOR`); no `settings.toml` plumbing, no Settings page UI.
- `heart-matrix-controller/event_log.py` (new) — append-only JSONL `EventLog` class with `append(event)` and `query(event_type=None, message_id=None, since=None)`. Backed by a file at `EVENT_LOG_PATH` (default `data/events.jsonl`).
- `heart-matrix-controller/main.py` — wire the renderer to write an event to the log immediately after each message advances, and replace the existing rotation loop with a call to `MessageSelector.pick(messages, now(), event_log)`.
- `heart-message-manager/static/event_log.py` (new, PyScript) — browser-side `EventLog` backed by `js.indexedDB`, mirroring the Pi-side schema so the same selector class works in the browser preview.
- `heart-message-manager/main.py` — the browser-preview seed path uses the same selector instance with the IndexedDB-backed event log so the dashboard preview agrees with the selector logic (it does NOT need to agree with the Pi's actual on-wall state — preview is illustrative).
- `heart-matrix-controller/settings.toml` — add only `EVENT_LOG_PATH` (default `data/events.jsonl`) and `EVENT_LOG_MAX_ENTRIES` (default 100) keys. The selector's tuning knobs live in code.
- A new test fixture under `tests/` for the selector: given a fixed clock, a known message set, and a known event log, the picked message is deterministic.
- No changes to: MQTT envelope, Twilio webhook auth, S3 backup, the server's `Message` model, the server's SQLite schema, the Pi's MQTT subscribe path, the Flask/PyScript shared-class pattern, the Flask Settings page, the Flask route handlers, or any template.