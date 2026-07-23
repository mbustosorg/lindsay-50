## 1. Dashboard Runtime Lifecycle

- [x] 1.1 Add browser-Python unit tests for lifecycle states, initial auto-start, Stop idempotence, and Stop-then-Start creating a new generation
- [x] 1.2 Implement/refactor the PyScript dashboard runtime controller with explicit generation records and `starting`/`running`/`stopping`/`stopped`/`error` state
- [x] 1.3 Add tests proving delayed REST, MQTT, status, and render callbacks from an old generation cannot mutate a newer generation
- [x] 1.4 Add generation-discriminator checks to every asynchronous dashboard-runtime callback and release callback proxies during teardown
- [x] 1.5 Add tests that Stop disconnects the MQTT subscription, releases the shared Python `MessageManager` and its in-memory ring, cancels rendering, discards the in-memory browser selector event log, releases timers/listeners, and prevents post-stop message/config dispatch
- [x] 1.6 Implement complete Stop teardown across `preview.js`, the MQTT-WS wrapper, `MessageManager`, coordinator/canvas bindings, in-memory browser selector event log, and Python callback proxies
- [x] 1.7 Add tests for partial startup failures at REST seed, MQTT connect, and coordinator construction, including cleanup and retry
- [x] 1.8 Implement failure cleanup and actionable error-state rendering; allow Start to create a clean retry generation

## 2. Fresh Runtime State and Persistence Removal

- [x] 2.1 Add tests that every fresh generation creates a new `MessageManager`/`InMemoryMessages`, a new in-memory browser selector event log, REST-seeds messages and config, and then connects a fresh MQTT client
- [x] 2.2 Refactor `app_main.py`/the dashboard controller from module-lifetime singletons to per-generation shared-Python runtime construction
- [x] 2.3 Add tests that initial load, refresh, and Stop-then-Start do not hydrate message/config state from `sessionStorage` or IndexedDB
- [x] 2.4 Remove the versioned `sessionStorage` message/config cache helpers and every-page hydrate/fallback bootstrap after the fresh seed path is in place
- [x] 2.5 Add tests that each fresh generation constructs a new in-memory browser selector event log and discards the previous generation's queue before message selection begins
- [x] 2.6 Add a bounded in-memory `EventLog` browser subclass (default cap 100 entries, FIFO drop-oldest) implementing the same `EventLog` contract the Pi's JSONL `EventLog` exposes, without changing the immutable `{event_type, message_id, timestamp, received_at}` schema
- [x] 2.7 Remove the prior `IndexedDBEventLog` browser mirror and its IndexedDB shim, and stop referencing IndexedDB from the dashboard runtime controller
- [x] 2.8 Update mirrored browser copies for every changed `lib_shared/` Python file and extend parity/manifest tests to prevent canonical/browser drift

## 3. In-Memory Browser Event Log

- [x] 3.1 Add unit tests covering the in-memory browser event log: `append` adds an immutable row, `query(event_type, message_id, since)` filters correctly, `last_for(message_id, event_type)` returns the most recent matching entry, and the bounded `deque(maxlen=N)` drops oldest at the cap (default 100)
- [x] 3.2 Implement a browser-side `EventLog` subclass backed by `collections.deque(maxlen=N)` matching the Pi JSONL `EventLog` contract; reuse the canonical immutable `{event_type, message_id, timestamp, received_at}` row schema
- [x] 3.3 Add tests that the in-memory log is empty immediately after Stop and immediately after fresh Start, and that the prior generation's log is not retained on the new generation
- [x] 3.4 Construct a fresh in-memory `EventLog` for every fresh runtime generation and release the prior generation's queue during Stop without keeping a reference
- [x] 3.5 Remove the prior `IndexedDBEventLog` browser mirror, its shim, and any references in `app_main.py` and the runtime controller

## 4. Flask Shell, Routes, and Asset Scope

- [x] 4.1 Add route/template tests that authenticated `/` contains the preview canvas, lifecycle controls, test injection, diagnostics, and recent-message region
- [x] 4.2 Recompose `dashboard.html` from the current dashboard, preview, and Testing-page components and make `/` the preview host
- [x] 4.3 Add tests that the sidebar is absent and Settings, Testing, and Messages links use `target="_blank" rel="noopener noreferrer"` while Logout retains its current behavior
- [x] 4.4 Remove the authenticated left sidebar and add explicit dashboard links without introducing in-place client routing
- [x] 4.5 Add tests that `/preview` redirects to the dashboard preview section and does not render a second simulator document
- [x] 4.6 Replace the standalone `/preview` render route with a compatibility redirect to `/`
- [x] 4.7 Add CSP tests proving `/` permits the required PyScript/WASM, MQTT-WebSocket, and configured media/S3 origins while unrelated routes do not receive the expanded policy
- [x] 4.8 Re-scope and rename the preview CSP hook/constants for the dashboard route
- [x] 4.9 Add template tests that simulated-Pi APP_CONFIG, PyScript entrypoints, preview JS, and message-topic MQTT bootstrap load only on `/`
- [x] 4.10 Make the base shell lightweight and dashboard-scope simulator assets while retaining the independent physical-sign status client on its supported pages

## 5. Dashboard Canvas and Controls

- [x] 5.1 Add UI/controller tests for control availability and status rendering in `starting`, `running`, `stopping`, `stopped`, and `error`
- [x] 5.2 Wire Start/Stop controls to the Python runtime controller, disable invalid transitions, and preserve the last frame only as a clearly stopped view
- [x] 5.3 Add regression tests that the existing canvas, fuzzy LED rendering, image/video overlays, effect name, current message, and diagnostics bridge work on `/`
- [x] 5.4 Move/reuse the preview canvas and browser-media overlay markup/runtime on the dashboard without forking the shared effect or scroller code
- [x] 5.5 Add initialization-order tests that the render loop cannot start before the dashboard Python runtime is ready
- [x] 5.6 Replace the current polling race between app and preview PyScript entrypoints with one deterministic ready hook/promise

## 6. Test Injection and Diagnostic Modals

- [x] 6.1 Add tests that dashboard injection reports Flask acceptance separately from matching MQTT dispatch and never creates an optimistic message on HTTP failure
- [x] 6.2 Reuse the existing `/api/test-messages` flow on the dashboard and correlate the accepted message with the running generation's live MQTT receipt
- [ ] 6.3 Add tests that injection while stopped reports Flask acceptance but no simulated-Pi MQTT receipt
- [x] 6.4 Gate MQTT-receipt UI on the active runtime generation rather than the POST response
- [x] 6.5 Add template/browser tests for accessible Current Config, Active Filters, and S3 Browser modal focus, Escape/close, and trigger-focus restoration
- [x] 6.6 Implement the three dashboard modals using the current runtime config/filter getters and existing authenticated S3 APIs
- [x] 6.7 Add tests that opening, updating, and closing each modal leaves runtime generation, render loop, coordinator state, and MQTT connection unchanged

## 7. Recent-100 Dashboard Message Management

- [x] 7.1 Add Python/browser-bridge tests that the dashboard requests up to 100 messages with suppressed records included and preserves `MessageView.source`, rules, media, sender, and display-time fields
- [x] 7.2 Wire the dashboard table directly to the shared Python `MessageManager` view and existing change-notification bridge without a parallel JavaScript message model
- [x] 7.3 Add client-pagination tests for 0, 1, 20, 21, and 100 records, including page clamping after live updates
- [x] 7.4 Implement 20-row client-side pagination over the existing 100-record in-memory ring without additional history requests
- [x] 7.5 Add rendering tests for distinct `REST seed` and `MQTT live` badges and suppression/rule indicators
- [x] 7.6 Reuse the existing `MessageView.source` and Testing-feed badge semantics in dashboard rows
- [x] 7.7 Add tests for single-flight suppress/unsuppress actions, successful authoritative refresh, and non-destructive error handling
- [x] 7.8 Implement dashboard suppress/unsuppress fetch actions without document reload or simulator reset

## 8. All-Message Archive

- [x] 8.1 Extend route tests to prove `/messages` returns all canonical SQLite records beyond 100, newest first, in server-controlled pages of 50
- [x] 8.2 Preserve/refine the SQLite-backed `/messages` pagination contract and safely handle malformed, below-range, and beyond-final page values
- [x] 8.3 Add template tests that archive rows retain media and suppression controls and expose matching `data-msg-id` and `data-received-at` attributes
- [x] 8.4 Add stable future-action row attributes to `messages.html` without adding permanent-delete UI or APIs
- [x] 8.5 Add tests that `/messages` does not load the simulated-Pi PyScript runtime or message-topic MQTT subscriber
- [x] 8.6 Keep the archive server-rendered and independent from the dashboard's 100-record browser ring

## 9. Secondary Pages and Transitional Testing

- [x] 9.1 Add regression tests that Settings and Testing retain their current supported forms, diagnostics, and auth behavior when opened directly
- [x] 9.2 Keep Settings and Testing functional as standalone pages while removing their dependency on the every-page simulated-Pi bootstrap
- [x] 9.3 Add browser tests that closing Settings, Testing, or Messages tabs does not stop or reset the original dashboard runtime
- [x] 9.4 Document Testing as transitional in the UI/docs without removing its route or template in this change
- [x] 9.5 Add regression tests that the physical-sign status pill/Settings health section still receive the physical status topic independently of simulator lifecycle

## 10. Verification and Check-In

- [x] 10.1 Bump every changed static JavaScript `?v=N` import in `templates/base.html` and add/update cache-buster assertions
- [x] 10.2 Run `PYTHONPATH=. pytest tests/ -v` and resolve all failures
- [x] 10.3 Run OpenSpec validation for `standalone-preview-dashboard` and resolve all schema/scenario errors
- [ ] 10.4 Exercise the dashboard in a real browser: initial auto-start, Stop, fresh Start, stale-callback rejection, REST seed, MQTT live receipt, refresh reset, and error retry
- [ ] 10.5 Exercise dashboard management in a real browser: source badges, 100-record pagination, suppression actions, test injection milestones, config/filter/S3 modals, and new-tab navigation
- [ ] 10.6 Exercise `/messages` with more than 100 fixtures: all-message count, 50-row pages, media, suppression, row hooks, invalid pages, and absence of delete controls
- [ ] 10.7 Verify the Pi's REST seed, native MQTT subscription, selector/JSONL `EventLog` behavior, and physical status publication remain unchanged
- [ ] 10.8 Archive `standalone-preview-dashboard` on the implementation branch only after all implementation and verification tasks are complete, then commit the archived specs before creating the PR