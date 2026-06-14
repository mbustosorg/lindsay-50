## REMOVED Requirements

### Requirement: Preview polls `/api/live-messages` every 3 s and shows new messages
**Reason**: Replaced by MQTT-driven updates. The browser now subscribes to the broker directly over WebSocket and runs the refactored `MessageManager`; the Flask `MessageManager`'s `/api/live-messages` polled endpoint is no longer the source of new-message signals for the preview. The WS connection is bootstrapped by the base template (not just `/preview`), so the message buffer is current on every admin page. See `browser-mqtt-websocket-client`, `browser-message-manager`, and `browser-message-buffer-persistence` for the new mechanism, and the new "Preview is driven by the in-browser MessageManager's on_message callback" requirement below.
**Migration**: The 3 s `setInterval(fetchLatestMessage, 3000)` loop in `static/preview.js` is replaced by reading from the in-memory state exposed by the base template's `app.js` (which owns the WS + `MessageManager` + IndexedDB). The `coordinator.request_message(body)` API stays unchanged — it is now invoked from the `MessageManager._on_message` callback instead of from a polled body diff. The `/api/live-messages` endpoint itself is removed (it was the Flask's server-side ring buffer; see `flask-server-cleanup`); the device and any REST clients continue to use the non-live `/api/messages` endpoint.

## ADDED Requirements

### Requirement: Preview is driven by the in-browser MessageManager's on_message callback

The preview SHALL update from new messages via the in-browser `MessageManager`. The base template's `app.js` (loaded on every admin page, not just `/preview`) instantiates a `MessageBufferStore`, the refactored `MessageManager`, and a `MqttWsClient`. After `MessageManager.seed()` populates the ring from `/api/messages` and `/api/config` (X-API-Key auth, same as the device), the WS connection opens and the `MqttWsClient` decodes each inbound `MessageEnvelope` and calls `MessageManager.dispatch(raw)`. The `MessageManager`'s `on_message` callback SHALL invoke `coordinator.request_message(body)` for each non-suppressed message. The preview SHALL NOT call `fetch('/api/live-messages...')` from `static/preview.js`; no `setInterval` against the Flask endpoint SHALL be scheduled.

#### Scenario: A new SMS arrival is reflected within the next MQTT delivery
- **WHEN** a Twilio webhook is received by Flask, the Flask publishes a `type: "message"` envelope to the broker, and the broker delivers the envelope to the browser's WS subscription
- **THEN** the browser SHALL decode the envelope, call `MessageManager.dispatch(raw)`, the `on_message` callback SHALL invoke `coordinator.request_message(body)`, and the canvas SHALL begin scrolling the new message body

#### Scenario: Duplicate envelopes are deduplicated by id
- **WHEN** the broker delivers two envelopes for the same `Message.id` (e.g. a replay or a duplicate publish)
- **THEN** the second `dispatch()` SHALL be a no-op for the in-memory ring (the `id` key collides in IndexedDB) and SHALL NOT invoke the `on_message` callback a second time

#### Scenario: A filtered envelope does not reach the canvas
- **WHEN** the broker delivers a `type: "message"` envelope whose sender / body matches an active `FilterRule` exclusion
- **THEN** the message SHALL be persisted to IndexedDB as suppressed; the `on_message` callback SHALL NOT be invoked; the canvas SHALL NOT change

#### Scenario: No polling against the Flask endpoint
- **WHEN** the preview page is open
- **THEN** `static/preview.js` SHALL NOT call `fetch('/api/live-messages...')`; the only network activity from the preview tab SHALL be the WebSocket connection to the broker, the one-time IndexedDB read on page load (or wipe+re-seed on app start / login / long-disconnect reconnect), and the seed `fetch('/api/messages')` + `fetch('/api/config')` calls

### Requirement: Preview shows the most recent message on page load, before the WS connects

On page load, after IndexedDB `hydrate()` completes, the preview SHALL render the most recent non-suppressed message from the hydrated ring buffer on the canvas. The MQTT-WS connection may not have completed by this point; the preview SHALL still display the most recent message because the hydrated state is the source of truth for the initial frame.

#### Scenario: First visit shows the idle state
- **WHEN** a user opens the preview page for the first time in a new browser profile and the wipe + re-seed sequence yields an empty `hydrate()` (no prior IndexedDB, no canonical messages on the server)
- **THEN** the preview SHALL render the idle state (background effect only, no scrolling text) until the first envelope is delivered over the WS connection

#### Scenario: Reload shows the last message immediately
- **WHEN** a user reloads the preview page after several messages have been dispatched and persisted
- **THEN** `hydrate()` SHALL populate the ring buffer; the preview SHALL render the most recent non-suppressed message on the canvas within the time the `requestAnimationFrame` loop takes to start; the canvas SHALL show this message before the WS connection completes

### Requirement: Preview exposes MQTT connection state alongside effect name and message body

The preview's status block SHALL display, in addition to the existing "currently active effect" and "now displaying message body" indicators, the MQTT-WS connection state: `connected`, `reconnecting`, `paused`, or `error`. The state SHALL update from the `MqttWsClient`'s status callback.

#### Scenario: Connection state updates on connect
- **WHEN** the WebSocket completes the MQTT CONNECT / SUBSCRIBE handshake
- **THEN** the status block SHALL display `Live` (or equivalent `connected` indicator); on disconnect it SHALL transition to `Reconnecting…` with the backoff visible or implied

#### Scenario: Hidden tab shows paused
- **WHEN** the user switches away from the preview tab
- **THEN** the status block SHALL display `Paused`; on return to the tab, after the WS resumes, the indicator SHALL return to `Live`

### Requirement: Preview reads message state from the base-template bootstrap, not its own state

The `/preview` page SHALL NOT instantiate its own `MessageManager` or `MqttWsClient`. It SHALL read message state from the module-scope objects exposed by the base template's `app.js`. The `/preview` page's PyScript code SHALL register a per-page `on_message` callback on the shared `MessageManager` (via `app.js`'s registration mechanism) and SHALL drive `PreviewCoordinator.request_message(body)` from that callback. Other pages (`/messages`, `/settings`, `/filters`, `/`) SHALL register their own per-page callbacks on the same shared `MessageManager`; the in-memory state is the single source of truth for new-message signals across the app.

#### Scenario: The /preview page reuses the base-template MessageManager
- **WHEN** the `/preview` page's PyScript init hook runs
- **THEN** it SHALL obtain the `MessageManager` instance from `app.js`'s module-scope export (or via a `window.App` object) and SHALL register its `on_message` callback on that shared instance; the page SHALL NOT construct a second `MessageManager` or a second `MqttWsClient`

#### Scenario: Navigating away from /preview does not break the buffer
- **WHEN** the operator navigates from `/preview` to `/messages`
- **THEN** the `/preview` page's per-page callback is unregistered (or simply stops being invoked because the page is unmounted); the new `/messages` page's base template bootstraps its own WS + `MessageManager`; the IndexedDB hydrate restores the same ring; the operator sees a consistent message history across pages
