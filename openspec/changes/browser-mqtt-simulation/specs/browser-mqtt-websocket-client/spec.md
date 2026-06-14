## ADDED Requirements

### Requirement: Browser connects to the MQTT broker over WebSocket

The browser SHALL open a WebSocket connection directly to the broker's MQTT-over-WebSocket endpoint (Paho or Adafruit IO, the same broker the device uses). The WebSocket SHALL authenticate using the broker credentials from the operator's `settings.toml` (`MQTT_USERNAME` / `MQTT_PASSWORD`) and SHALL subscribe to the configured topic (`MQTT_TOPIC`). The browser SHALL NOT relay through Flask — no new Flask WebSocket endpoint, no new Flask route, no `Flask-Sock` dependency.

The connection SHALL use the WebSocket port exposed by the broker (Paho: `ws://<host>:9001/mqtt`; Adafruit IO: `wss://io.adafruit.com/mqtt`). The browser SHALL read the WS URL from a `MQTT_WS_URL` config value, with operator-overridable defaults that mirror the device's `MQTT_HOST`.

The WS connection SHALL be established by the base template's `app.js` on every admin page load (not just `/preview`), so the message buffer is current on every page. The `/preview` page adds the PyScript coordinator and canvas on top of the same shared in-memory state.

#### Scenario: First page load of an admin route opens one WebSocket

- **WHEN** a user opens any admin page (`/`, `/messages`, `/settings`, `/filters`, `/preview`, `/testing`, etc.) and the base template's `app.js` has finished bootstrapping
- **THEN** exactly one WebSocket connection SHALL be opened from the browser tab to the broker's `MQTT_WS_URL`, authenticated with `MQTT_USERNAME` / `MQTT_PASSWORD`, and subscribed to `MQTT_TOPIC`

#### Scenario: No Flask WebSocket route is created

- **WHEN** the change is deployed
- **THEN** `heart-message-manager/main.py` SHALL NOT register any new WebSocket route or SSE stream; the only WebSocket opened by the preview tab is the direct connection to the broker

#### Scenario: Connection uses the same credentials the device uses

- **WHEN** the operator configures `MQTT_USERNAME`, `MQTT_PASSWORD`, and `MQTT_TOPIC` in `settings.toml`
- **THEN** the browser SHALL use those same values to authenticate and subscribe; the device and the browser SHALL be independent subscribers on the same topic and broker

### Requirement: MQTT-WS client decodes MessageEnvelope payloads and emits them to the in-browser MessageManager

The browser-side MQTT-WS client SHALL decode each inbound PUBLISH payload as UTF-8 JSON in the shape of `MessageEnvelope` and SHALL call the in-browser `MessageManager.dispatch(raw)` method with the raw decoded string. The client SHALL handle both `message` and `config` envelope types; the dispatcher's contract (from the refactored `lib_shared/message_manager.py`) routes them to the ring buffer and `SignConfig` respectively.

#### Scenario: Inbound message envelope is dispatched

- **WHEN** the broker delivers a `MessageEnvelope` with `type: "message"` on the subscribed topic
- **THEN** the browser-side MQTT-WS client SHALL decode the payload and call `MessageManager.dispatch(raw)` exactly once per delivery

#### Scenario: Inbound config envelope is dispatched

- **WHEN** the broker delivers a `MessageEnvelope` with `type: "config"` on the subscribed topic
- **THEN** the browser-side MQTT-WS client SHALL decode the payload and call `MessageManager.dispatch(raw)` exactly once per delivery; the dispatcher's config-handling path updates the in-browser `SignConfig`

#### Scenario: Malformed payload is logged and dropped

- **WHEN** the broker delivers a payload that is not valid JSON or that does not parse as a `MessageEnvelope`
- **THEN** the client SHALL log a warning and SHALL NOT call `MessageManager.dispatch`; the previous ring buffer / config state SHALL be unchanged

### Requirement: Auto-reconnect with exponential backoff on disconnect

The browser-side MQTT-WS client SHALL reconnect automatically on disconnect, with exponential backoff starting at 1 second and capped at 60 seconds. Reconnect attempts SHALL stop on success and SHALL resume on the next disconnect. The client SHALL surface connection state (`connected` / `reconnecting` / `paused` / `error`) to a callback the preview UI uses to render a status indicator.

The `paused` transition (see the next requirement) is event-driven by the elapsed disconnect duration; the wipe + re-seed on the subsequent `connected` event is the recovery mechanism (per the `browser-message-buffer-persistence` capability).

#### Scenario: Network drop triggers reconnect

- **WHEN** the WebSocket connection drops
- **THEN** the client SHALL attempt to reconnect with backoff (1s → 2s → 4s → 8s → 16s → 32s → 60s) and SHALL emit `reconnecting` to the status callback on each attempt

#### Scenario: Reconnect resumes dispatch on success

- **WHEN** the client successfully reconnects and resubscribes
- **THEN** subsequent PUBLISH frames SHALL be decoded and dispatched as before; the status callback SHALL emit `connected`

#### Scenario: Reconnect backoff is bounded

- **WHEN** the WebSocket repeatedly fails to reconnect
- **THEN** the backoff SHALL cap at 60 seconds; the client SHALL continue attempting at that interval, not longer

#### Scenario: Reconnect after short disconnect does not trigger wipe + re-seed

- **WHEN** the WebSocket reconnects and the disconnect duration was below the threshold
- **THEN** the IndexedDB SHALL NOT be cleared; the WS SHALL resume and append new envelopes to the existing in-memory ring; missed envelopes during the short disconnect window are simply lost from the in-memory ring (broker's at-most-once delivery semantics)

### Requirement: Status transitions to `paused` after a long disconnect; `paused` → `connected` triggers wipe + re-seed

The browser-side MQTT-WS client SHALL track its own `lastConnectedAt` timestamp. When the elapsed time since the last `connected` event exceeds a configurable threshold (default 5 minutes, set via `window.APP_CONFIG.mqttLongDisconnectMs` from the base template), the client SHALL emit a `paused` status event to the status callback.

The `paused` state is the recovery signal: when the client subsequently reconnects after a `paused` window, the `connected` event SHALL carry `wasLongDisconnect: true`, and the base template's `app.js` SHALL trigger a wipe + re-seed of the IndexedDB (per the `browser-message-buffer-persistence` capability) before the new envelopes are dispatched.

The client SHALL NOT install a `visibilitychange` listener. The OS/browser naturally throttles or closes background WebSockets; the elapsed-time → `paused` transition covers all paths to a long disconnect (visibility, network drop, broker crash, laptop sleep, mobile background) with one mechanism. There is no separate code path for "the user switched tabs."

#### Scenario: Long disconnect transitions to paused

- **WHEN** the WebSocket connection drops and the elapsed time since the last `connected` event exceeds the threshold (default 5 minutes)
- **THEN** the client SHALL emit `paused` to the status callback; the UI status block SHALL show `Paused — will re-seed on reconnect (Xm elapsed)` with the elapsed time visible; reconnect attempts SHALL continue (the OS will eventually allow the WS to reopen)

#### Scenario: Reconnect after paused carries wasLongDisconnect

- **WHEN** the WebSocket successfully reconnects after the `paused` state was reached
- **THEN** the `connected` event SHALL carry `wasLongDisconnect: true`; the base template's `app.js` SHALL clear the IndexedDB `messages` and `config` stores, re-seed from `/api/messages` and `/api/config` (X-API-Key auth, same as the device), and resume envelope dispatch; subsequent envelopes SHALL be appended to the freshly-seeded ring

#### Scenario: Short disconnect does not transition to paused

- **WHEN** the WebSocket drops and reconnects within the threshold
- **THEN** the client SHALL NOT emit `paused`; the `connected` event SHALL NOT carry `wasLongDisconnect: true`; the IndexedDB SHALL NOT be cleared; subsequent envelopes SHALL be appended to the existing in-memory ring

#### Scenario: Threshold is configurable

- **WHEN** the operator sets `mqtt_long_disconnect_ms` (or the equivalent env / settings key) to a value other than the default 300000 ms
- **THEN** the threshold used by the WS client SHALL match the configured value; the `paused` transition SHALL fire at the configured threshold; the wipe + re-seed on reconnect SHALL use the same threshold

#### Scenario: No visibilitychange listener is installed

- **WHEN** the WS shim initializes
- **THEN** the shim SHALL NOT call `document.addEventListener('visibilitychange', ...)`; the only listeners the shim installs are on the WebSocket object itself (`onopen`, `onclose`, `onerror`, `onmessage`) and the disconnect-duration timer that drives the `paused` transition
