# Tasks: weighted-message-selection

## 1. Event Log (Pi-local)

- [ ] 1.1 Define the event schema: `event_type` (string, discriminator), `message_id` (string), `timestamp` (float epoch seconds), `sent_at` (float, denormalized), `favorite` (bool, denormalized)
- [ ] 1.2 Implement `EventLog` class in `heart-matrix-controller/event_log.py` with `append(event)` and `query(event_type=None, message_id=None, since=None) -> Iterator[dict]` methods, backed by an append-only JSONL file at `EVENT_LOG_PATH` (default `data/events.jsonl`)
- [ ] 1.3 In-memory cache: load the log into memory at boot and on every append (write-through). The selector reads from the cache, not the file directly
- [ ] 1.4 Corrupt-line tolerance: the reader MUST skip any line that fails JSON parsing and log a warning. One bad line MUST NOT lose other events
- [ ] 1.5 Rotation: when the active file exceeds 10 MB or 30 days, archive to `events.jsonl.<UTC-date>.gz` (gzip) and start a fresh active file. Retain archives for 90 days, then delete
- [ ] 1.6 Add `EVENT_LOG_PATH` (default `data/events.jsonl`), `EVENT_LOG_ROTATE_BYTES` (default 10485760), `EVENT_LOG_ROTATE_DAYS` (default 30), `EVENT_LOG_ARCHIVE_DAYS` (default 90) keys to `heart-matrix-controller/settings.toml`

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

- [ ] 4.1 Wire the renderer in `heart-matrix-controller/main.py` to call `event_log.append({...})` immediately after each message advances, passing the current pattern's `event_type` (e.g., `text_display` for the scroller; future `image_display` for image patterns)
- [ ] 4.2 Replace the existing first-in/first-out rotation loop with `selector.pick(messages, time.time(), event_log)`
- [ ] 4.3 Confirm the MQTT subscribe callback in `lib_shared/mqtt_factory.py` / `heart-matrix-controller/main.py` still bypasses the selector for new envelopes (pre-emption invariant) AND does NOT write a `text_display` event for the pre-empting message
- [ ] 4.4 Optional: the renderer MAY write a `preempted` event (distinct event_type) for debug visibility

## 5. Configuration

- [ ] 5.1 Add `SELECTOR_W_DISPLAY` (default 0.6), `SELECTOR_W_SEND` (default 0.3), `SELECTOR_W_FAVORITE` (default 0.4), `SELECTOR_SATURATION_SECONDS` (default 86400), `SELECTOR_OFFSET_SECONDS` (default 1209600), and `USE_WEIGHTED_SELECTOR` (default false) keys to both `heart-message-manager/settings.toml` and `heart-matrix-controller/settings.toml`
- [ ] 5.2 Plumb the keys through `lib_shared/config_reader.py` so the selector reads them at construction time
- [ ] 5.3 Update `settings.toml.example` in both `heart-message-manager/` and `heart-matrix-controller/` with the new keys and inline comments

## 6. Tests

- [ ] 6.1 Unit test: `EventLog.append` writes a parseable JSONL line and updates the in-memory cache
- [ ] 6.2 Unit test: `EventLog.query(event_type="text_display")` returns only events with that event_type
- [ ] 6.3 Unit test: `EventLog.query(message_id="X")` returns only events for X
- [ ] 6.4 Unit test: a corrupt JSONL line is skipped without breaking subsequent reads
- [ ] 6.5 Unit test: log rotation — when file exceeds `EVENT_LOG_ROTATE_BYTES`, archive is created and old events stop counting toward `display_recency`
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
- [ ] 6.17 Integration test: browser preview (`/playful`) uses the same `MessageSelector` class and produces a deterministic pick from an IndexedDB-backed event log (manual smoke test acceptable)

## 7. Migration & Rollout

- [ ] 7.1 No database migration needed. The event log is a new file; the server's `Message` model and SQLite schema are unchanged
- [ ] 7.2 Add a feature flag `USE_WEIGHTED_SELECTOR` (default false) in `settings.toml` so the new selector ships behind a toggle and the old first-in/first-out code path remains intact
- [ ] 7.3 Document the rollout sequence in `README.md`: enable on a test Pi first, observe for at least one full `OFFSET_SECONDS` window (14 days by default) before enabling globally
- [ ] 7.4 Document the rollback path: flip `USE_WEIGHTED_SELECTOR=false` and restart; the previous rotation code path remains intact

## 8. Documentation

- [ ] 8.1 Add `docs/event-log.md` describing the JSONL schema, the rotation policy, and how to read/filter the log for debugging (`rg '"event_type": "text_display"' data/events.jsonl`)
- [ ] 8.2 Update the project `CLAUDE.md` `## Architecture` section to mention the event log lives at `heart-matrix-controller/data/events.jsonl` and is the selector's source of truth for display-recency
- [ ] 8.3 Note in the docs that future work may publish events to MQTT for remote debugging; the schema is forward-compatible (each event has `event_type`, `message_id`, `timestamp`, `sent_at`, `favorite`) but no MQTT code ships in this change

## 9. Future Work (Out of Scope for This Change)

- [ ] 9.1 Publish events to an MQTT topic (e.g., `sign/events`) for remote debugging
- [ ] 9.2 Server-side event log mirroring the Pi's via MQTT subscription, stored in SQLite or a sidecar file
- [ ] 9.3 Browser preview reads the server-mirrored events instead of its own IndexedDB log
- [ ] 9.4 Admin UI for showing the selector's reasoning on the dashboard (would require either a sidecar debug log or a per-pick annotation in the event log)