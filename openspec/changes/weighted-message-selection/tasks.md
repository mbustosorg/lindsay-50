# Tasks: weighted-message-selection

## 1. Event Log (Pi-local)

- [x] 1.1 Define the event schema: `event_type` (string, discriminator), `message_id` (string), `timestamp` (float epoch seconds), `received_at` (float, denormalized). The schema MUST NOT carry mutable current-state fields such as `favorite`
- [x] 1.2 Implement `EventLog` class in `heart-matrix-controller/event_log.py` with `append(event)` and `query(event_type=None, message_id=None, since=None) -> Iterator[dict]` methods, backed by an append-only JSONL file at `EVENT_LOG_PATH` (default `data/events.jsonl`)
- [x] 1.3 In-memory cache: load the log into memory at boot and on every append (write-through). The selector reads from the cache, not the file directly
- [x] 1.4 Corrupt-line tolerance: the reader MUST skip any line that fails JSON parsing and log a warning. One bad line MUST NOT lose other events
- [x] 1.5 Bounded ring: when the log has `EVENT_LOG_MAX_ENTRIES` entries, appending a new event MUST drop the oldest entry and rewrite the on-disk file with the N most recent entries (in append order). No archive, no compression
- [x] 1.6 Add `EVENT_LOG_PATH` (default `data/events.jsonl`) and `EVENT_LOG_MAX_ENTRIES` (default 100) keys to `heart-matrix-controller/settings.toml`

## 2. Browser Event Log (IndexedDB)

- [x] 2.1 Implement `IndexedDBEventLog` PyScript wrapper at `heart-message-manager/event_log.py` with `append(event)` and `query(event_type=None, message_id=None)` matching the Pi-side schema. Backed by `js.indexedDB`
- [x] 2.2 Ensure the IndexedDB-backed log is per-browser (not synced across browsers). Document preview-is-illustrative in the spec
- [x] 2.3 Wire the browser preview (`/playful`) to construct the IndexedDB event log at boot and pass it to the selector

## 3. Selector Implementation

- [x] 3.1 Add `MessageSelector` class in `lib_shared/selector.py` with signature `pick(messages: list[Message], now: float, event_log: EventLog) -> Optional[Message]`
- [x] 3.2 Implement `display_recency(message, now, event_log, current_event_type)` per design Decision 2 - reads the most recent matching event from the log, returns 1.0 if none
- [x] 3.3 Implement `send_recency(message, eligible_set, now)` per design Decision 3
- [x] 3.4 Implement the eligibility filter (`now - received_at <= OFFSET_SECONDS`) per design Decision 4
- [x] 3.5 Implement the additive weighted score per design Decision 1
- [x] 3.6 Implement the favorite boost per design Decision 1 (additive weight OR `received_at` clamp - implementer chooses and documents)
- [x] 3.7 Implement the deterministic tie-breaker per design Decision 6 (`(-score, received_at, message.id)`)

## 4. Renderer Wiring (Pi)

- [x] 4.1 Wire the renderer in `heart-matrix-controller/main.py` to call `event_log.append({"event_type": current_pattern_event_type, "message_id": msg.id, "timestamp": time.time(), "received_at": msg.received_at})` immediately after each message advances. Do NOT include `favorite` in the event payload - favorite is read from the message record at pick time
- [x] 4.2 Replace the existing first-in/first-out rotation loop with `selector.pick(messages, time.time(), event_log)`
- [x] 4.3 Confirm the MQTT subscribe callback in `lib_shared/mqtt_factory.py` / `heart-matrix-controller/main.py` still bypasses the selector for new envelopes (pre-emption invariant) AND does NOT write a `text_display` event for the pre-empting message
- [x] 4.4 Optional: the renderer MAY write a `preempted` event (distinct event_type) for debug visibility (deferred; not needed for the rollout)

## 5. Configuration (code constants)

- [x] 5.1 Define module-level constants at the top of `lib_shared/selector.py`: `W_DISPLAY = 0.6`, `W_SEND = 0.3`, `W_FAVORITE = 0.4`, `SATURATION_SECONDS = 86400`, `OFFSET_SECONDS = 1209600`, `USE_WEIGHTED_SELECTOR = False`
- [x] 5.2 The selector reads these constants at construction time (no `lib_shared/config_reader` plumbing required; no env-var override)
- [x] 5.3 `OFFSET_SECONDS` is seconds-denominated to make unit tests trivial (e.g., set to `60` for a one-minute window in a test). The eventual operator-facing presentation on the admin UI is days/hours; that translation is a separate change
- [x] 5.4 Add only `EVENT_LOG_PATH` (default `data/events.jsonl`) and `EVENT_LOG_MAX_ENTRIES` (default 100) keys to `heart-matrix-controller/settings.toml` and the corresponding `.example` file. No `SELECTOR_*` keys, no `USE_WEIGHTED_SELECTOR` key - those live in code
- [x] 5.5 No changes to `heart-message-manager/settings.toml`, the Flask app, the Settings template, or any route handler

## 6. Tests

- [x] 6.1 Unit test: `EventLog.append` writes a parseable JSONL line and updates the in-memory cache
- [x] 6.2 Unit test: `EventLog.query(event_type="text_display")` returns only events with that event_type
- [x] 6.3 Unit test: `EventLog.query(message_id="X")` returns only events for X
- [x] 6.4 Unit test: a corrupt JSONL line is skipped without breaking subsequent reads
- [x] 6.5 Unit test: bounded ring - when the log has `EVENT_LOG_MAX_ENTRIES` entries and a new event is appended, the oldest entry is dropped and the on-disk file holds exactly N entries
- [x] 6.6 Unit test: `display_recency` returns 1.0 for a message with no matching event
- [x] 6.7 Unit test: `display_recency` for a message with a recent event returns a value < 1.0
- [x] 6.8 Unit test: `display_recency` is per-event-type - a `text_display` event does not reduce the display-recency seen by an `image_display` selector
- [x] 6.9 Unit test: `send_recency` returns 1.0 for the newest eligible message and 0.0 for the oldest
- [x] 6.10 Unit test: messages older than `OFFSET_SECONDS` are excluded from the eligible set (use a small `OFFSET_SECONDS` value, e.g., 60 seconds, in the test)
- [x] 6.11 Unit test: a favorite with the same recency as a non-favorite beats the non-favorite
- [x] 6.12 Unit test: deterministic pick - same messages + same clock + same event log returns the same message
- [x] 6.13 Unit test: stable tie-breaker - two messages with identical scores resolve by `(received_at, id)`
- [x] 6.14 Unit test: log survives a restart - write events, instantiate a fresh `EventLog`, query returns the prior events
- [x] 6.15 Integration test: renderer writes an event after advancing; subsequent selector invocation sees the updated display-recency
- [x] 6.16 Integration test: a new SMS arriving during the rotation pre-empts the next selector pick AND does NOT write a `text_display` event for the pre-empting message
- [x] 6.17 Unit test: event schema contains exactly `{event_type, message_id, timestamp, received_at}` - adding `favorite` (or any other mutable field) fails the test
- [x] 6.18 Unit test: the selector's constants are readable from `lib_shared/selector.py` (e.g., `from lib_shared.selector import W_DISPLAY, W_SEND, W_FAVORITE, SATURATION_SECONDS, OFFSET_SECONDS, USE_WEIGHTED_SELECTOR`) and have the documented defaults
- [x] 6.19 Integration test: browser preview (`/playful`) uses the same `MessageSelector` class and produces a deterministic pick from an IndexedDB-backed event log (manual smoke test acceptable)

## 7. Migration & Rollout

- [x] 7.1 No database migration needed. The event log is a new file; the server's `Message` model and SQLite schema are unchanged
- [x] 7.2 Set `USE_WEIGHTED_SELECTOR = False` (the code constant) so the new selector ships dark and the old first-in/first-out code path remains intact. Flip the constant to `True` and redeploy to enable
- [x] 7.3 Document the rollout sequence: enable on a test Pi first, observe for at least one full `OFFSET_SECONDS` window (14 days by default) before enabling globally — see `docs/event-log.md`
- [x] 7.4 Document the rollback path: set `USE_WEIGHTED_SELECTOR = False` and redeploy; the previous rotation code path remains intact

## 8. Documentation

- [x] 8.1 Add `docs/event-log.md` describing the JSONL schema, the bounded-ring policy (default 100 entries), and how to read/filter the log for debugging (`rg '"event_type": "text_display"' data/events.jsonl`)
- [x] 8.2 Update the project `CLAUDE.md` `## Architecture` section to mention the event log lives at `heart-matrix-controller/data/events.jsonl` and is the selector's source of truth for display-recency
- [x] 8.3 Note in the docs that future work may publish events to MQTT for remote debugging; the schema is forward-compatible (each event has `event_type`, `message_id`, `timestamp`, `received_at`) but no MQTT code ships in this change
- [x] 8.4 Note in `lib_shared/selector.py` (docstring or inline comment at the constants block) that the weights/window/saturation are behavioral knobs that operators tune by editing the source; only `EVENT_LOG_PATH` and `EVENT_LOG_MAX_ENTRIES` are operational values in `settings.toml`

## 9. Future Work (Out of Scope for This Change)

- [ ] 9.1 Publish events to an MQTT topic (e.g., `sign/events`) for remote debugging
- [ ] 9.2 Server-side event log mirroring the Pi's via MQTT subscription, stored in SQLite or a sidecar file
- [ ] 9.3 Browser preview reads the server-mirrored events instead of its own IndexedDB log
- [ ] 9.4 Admin UI for showing the selector's reasoning on the dashboard (would require either a sidecar debug log or a per-pick annotation in the event log)
- [ ] 9.5 Storage choice for the `favorite` flag: implementer chooses between `Message.favorite` field or a separate config-side favorites list (similar to filters)
- [ ] 9.6 Operator-facing UI for the eligibility window on the Settings page (or a new Message Rotation page). Translates between the seconds-denominated `OFFSET_SECONDS` constant and a days/hours input. May also expose the three weights and `SATURATION_SECONDS` once the algorithm stabilizes. Defer until operators express the need.
