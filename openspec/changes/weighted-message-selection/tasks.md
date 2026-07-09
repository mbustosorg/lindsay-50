# Tasks: weighted-message-selection

## 1. Event Log (Pi-local)

- [ ] 1.1 Define the event schema: `event_type` (string, discriminator), `message_id` (string), `timestamp` (float epoch seconds), `sent_at` (float, denormalized). The schema MUST NOT carry mutable current-state fields such as `favorite`
- [ ] 1.2 Implement `EventLog` class in `heart-matrix-controller/event_log.py` with `append(event)` and `query(event_type=None, message_id=None, since=None) -> Iterator[dict]` methods, backed by an append-only JSONL file at `EVENT_LOG_PATH` (default `data/events.jsonl`)
- [ ] 1.3 In-memory cache: load the log into memory at boot and on every append (write-through). The selector reads from the cache, not the file directly
- [ ] 1.4 Corrupt-line tolerance: the reader MUST skip any line that fails JSON parsing and log a warning. One bad line MUST NOT lose other events
- [ ] 1.5 Bounded ring: when the log has `EVENT_LOG_MAX_ENTRIES` entries, appending a new event MUST drop the oldest entry and rewrite the on-disk file with the N most recent entries (in append order). No archive, no compression
- [ ] 1.6 Add `EVENT_LOG_PATH` (default `data/events.jsonl`) and `EVENT_LOG_MAX_ENTRIES` (default 100) keys to `heart-matrix-controller/settings.toml`

## 2. Browser Event Log (IndexedDB)

- [ ] 2.1 Implement `IndexedDBEventLog` PyScript wrapper at `heart-message-manager/static/event_log.py` with `append(event)` and `query(event_type=None, message_id=None)` matching the Pi-side schema. Backed by `js.indexedDB`
- [ ] 2.2 Ensure the IndexedDB-backed log is per-browser (not synced across browsers). Document preview-is-illustrative in the spec
- [ ] 2.3 Wire the browser preview (`/playful`) to construct the IndexedDB event log at boot and pass it to the selector

## 3. Selector Implementation

- [ ] 3.1 Add `MessageSelector` class in `lib_shared/selector.py` with signature `pick(messages: list[Message], now: float, event_log: EventLog) -> Optional[Message]`
- [ ] 3.2 Implement `display_recency(message, now, event_log, current_event_type)` per design Decision 2 — reads the most recent matching event from the log, returns 1.0 if none
- [ ] 3.3 Implement `send_recency(message, eligible_set, now)` per design Decision 3
- [ ] 3.4 Implement the eligibility filter (`now − sent_at <= offset_seconds`) per design Decision 4
- [ ] 3.5 Implement the additive weighted score per design Decision 1
- [ ] 3.6 Implement the favorite boost per design Decision 1 (additive weight OR `sent_at` clamp — implementer chooses and documents)
- [ ] 3.7 Implement the deterministic tie-breaker per design Decision 6 (`(-score, sent_at, message.id)`)

## 4. Renderer Wiring (Pi)

- [ ] 4.1 Wire the renderer in `heart-matrix-controller/main.py` to call `event_log.append({"event_type": current_pattern_event_type, "message_id": msg.id, "timestamp": time.time(), "sent_at": msg.sent_at})` immediately after each message advances. Do NOT include `favorite` in the event payload — favorite is read from the message record at pick time
- [ ] 4.2 Replace the existing first-in/first-out rotation loop with `selector.pick(messages, time.time(), event_log)`
- [ ] 4.3 Confirm the MQTT subscribe callback in `lib_shared/mqtt_factory.py` / `heart-matrix-controller/main.py` still bypasses the selector for new envelopes (pre-emption invariant) AND does NOT write a `text_display` event for the pre-empting message
- [ ] 4.4 Optional: the renderer MAY write a `preempted` event (distinct event_type) for debug visibility

## 5. Configuration

- [ ] 5.1 Add `SELECTOR_W_DISPLAY` (default 0.6), `SELECTOR_W_SEND` (default 0.3), `SELECTOR_W_FAVORITE` (default 0.4), `SELECTOR_SATURATION_SECONDS` (default 86400), `SELECTOR_OFFSET_SECONDS` (default 1209600), and `USE_WEIGHTED_SELECTOR` (default false) keys to both `heart-message-manager/settings.toml` and `heart-matrix-controller/settings.toml`
- [ ] 5.2 Plumb the keys through `lib_shared/config_reader.py` so the selector reads them at construction time
- [ ] 5.3 Update `settings.toml.example` in both `heart-message-manager/` and `heart-matrix-controller/` with the new keys and inline comments
- [ ] 5.4 The eligibility window and the three selector weights are ALSO surfaced on the Settings page (see Section 10) so operators can tune them without editing `settings.toml`

## 6. Tests

- [ ] 6.1 Unit test: `EventLog.append` writes a parseable JSONL line and updates the in-memory cache
- [ ] 6.2 Unit test: `EventLog.query(event_type="text_display")` returns only events with that event_type
- [ ] 6.3 Unit test: `EventLog.query(message_id="X")` returns only events for X
- [ ] 6.4 Unit test: a corrupt JSONL line is skipped without breaking subsequent reads
- [ ] 6.5 Unit test: bounded ring — when the log has `EVENT_LOG_MAX_ENTRIES` entries and a new event is appended, the oldest entry is dropped and the on-disk file holds exactly N entries
- [ ] 6.6 Unit test: `display_recency` returns 1.0 for a message with no matching event
- [ ] 6.7 Unit test: `display_recency` for a message with a recent event returns a value < 1.0
- [ ] 6.8 Unit test: `display_recency` is per-event-type — a `text_display` event does not reduce the display-recency seen by an `image_display` selector
- [ ] 6.9 Unit test: `send_recency` returns 1.0 for the newest eligible message and 0.0 for the oldest
- [ ] 6.10 Unit test: messages older than `offset_seconds` are excluded from the eligible set
- [ ] 6.11 Unit test: a favorite with the same recency as a non-favorite beats the non-favorite
- [ ] 6.12 Unit test: deterministic pick — same messages + same clock + same event log returns the same message
- [ ] 6.13 Unit test: stable tie-breaker — two messages with identical scores resolve by `(sent_at, id)`
- [ ] 6.14 Unit test: log survives a restart — write events, instantiate a fresh `EventLog`, query returns the prior events
- [ ] 6.15 Integration test: renderer writes an event after advancing; subsequent selector invocation sees the updated display-recency
- [ ] 6.16 Integration test: a new SMS arriving during the rotation pre-empts the next selector pick AND does NOT write a `text_display` event for the pre-empting message
- [ ] 6.17 Unit test: event schema contains exactly `{event_type, message_id, timestamp, sent_at}` — adding `favorite` (or any other mutable field) fails the test
- [ ] 6.17 Integration test: browser preview (`/playful`) uses the same `MessageSelector` class and produces a deterministic pick from an IndexedDB-backed event log (manual smoke test acceptable)

## 7. Migration & Rollout

- [ ] 7.1 No database migration needed. The event log is a new file; the server's `Message` model and SQLite schema are unchanged
- [ ] 7.2 Add a feature flag `USE_WEIGHTED_SELECTOR` (default false) in `settings.toml` so the new selector ships behind a toggle and the old first-in/first-out code path remains intact
- [ ] 7.3 Document the rollout sequence in `README.md`: enable on a test Pi first, observe for at least one full `OFFSET_SECONDS` window (14 days by default) before enabling globally
- [ ] 7.4 Document the rollback path: flip `USE_WEIGHTED_SELECTOR=false` and restart; the previous rotation code path remains intact

## 8. Documentation

- [ ] 8.1 Add `docs/event-log.md` describing the JSONL schema, the bounded-ring policy (default 100 entries), and how to read/filter the log for debugging (`rg '"event_type": "text_display"' data/events.jsonl`)
- [ ] 8.2 Update the project `CLAUDE.md` `## Architecture` section to mention the event log lives at `heart-matrix-controller/data/events.jsonl` and is the selector's source of truth for display-recency
- [ ] 8.3 Note in the docs that future work may publish events to MQTT for remote debugging; the schema is forward-compatible (each event has `event_type`, `message_id`, `timestamp`, `sent_at`) but no MQTT code ships in this change

## 9. Future Work (Out of Scope for This Change)

- [ ] 9.1 Publish events to an MQTT topic (e.g., `sign/events`) for remote debugging
- [ ] 9.2 Server-side event log mirroring the Pi's via MQTT subscription, stored in SQLite or a sidecar file
- [ ] 9.3 Browser preview reads the server-mirrored events instead of its own IndexedDB log
- [ ] 9.4 Admin UI for showing the selector's reasoning on the dashboard (would require either a sidecar debug log or a per-pick annotation in the event log)
- [ ] 9.5 Storage choice for the `favorite` flag: implementer chooses between `Message.favorite` field or a separate config-side favorites list (similar to filters)

## 10. Settings Page UI

- [ ] 10.1 Add a "Message rotation" section to the Settings page (both the original and playful variants) that exposes the eligibility window (labeled in days) and the three selector weights (`W_DISPLAY`, `W_SEND`, `W_FAVORITE`)
- [ ] 10.2 The eligibility-window control is a numeric input (default 14 days) that persists to `SELECTOR_OFFSET_SECONDS` via the existing settings-update endpoint
- [ ] 10.3 The weight controls are sliders or numeric inputs (defaults 0.6 / 0.3 / 0.4) that persist to `SELECTOR_W_DISPLAY` / `SELECTOR_W_SEND` / `SELECTOR_W_FAVORITE` respectively
- [ ] 10.4 Validate the controls: window must be positive integer days; weights must be non-negative floats (zero is valid for "disable this component")
- [ ] 10.5 Update the playful-settings page (`heart-message-manager/templates/playful-settings*.html`) to mirror the same controls with the playful visual treatment