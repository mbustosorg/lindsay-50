## ADDED Requirements

### Requirement: Flask process does not host a MessageManager instance

The Flask process SHALL NOT instantiate `MessageManager` at boot. The `from lib_shared.message_manager import MessageManager` import SHALL be removed from `heart-message-manager/main.py`. The Flask process SHALL NOT call `MessageManager.seed()`, SHALL NOT construct an MQTT client via `make_mqtt_client(_message_mgr.dispatch)`, and SHALL NOT maintain an in-memory ring buffer of received messages or a live `SignConfig` snapshot.

The `lib_shared/message_manager.py` module itself SHALL remain — the device (`heart-matrix-controller/main.py`) imports it directly. As part of the `browser-message-manager` capability, the `MessageManager` class is also refactored to take `(messages_api_url, config_api_url, api_key, is_browser=False, on_message=None)` as constructor parameters, with both I/O paths (`requests` for the device, `js.fetch` for the browser) implemented internally and selected by the `is_browser` flag (no more `config_reader` import at module top, no module-top `import requests` or `import js`). The Flask's removal of its instance is independent of that refactor; the device's call site is updated to pass the new constructor arguments.

#### Scenario: main.py no longer imports MessageManager
- **WHEN** the change is deployed
- **THEN** `heart-message-manager/main.py` SHALL NOT contain `from lib_shared.message_manager import MessageManager`; the `MessageManager` symbol SHALL NOT be referenced anywhere in the Flask process

#### Scenario: main.py no longer constructs an MQTT client
- **WHEN** the change is deployed
- **THEN** `heart-message-manager/main.py` SHALL NOT contain `make_mqtt_client`; no MQTT client SHALL be constructed or started in the Flask process

#### Scenario: main.py no longer calls MessageManager.seed
- **WHEN** the change is deployed
- **THEN** no daemon thread SHALL call `_message_mgr.seed()` at Flask boot; the Flask process SHALL NOT perform a REST-based message or config seed on startup

#### Scenario: lib_shared/message_manager.py is updated for the `is_browser` flag with internal I/O paths
- **WHEN** the change is deployed
- **THEN** `lib_shared/message_manager.py` SHALL still export the `MessageManager` class with the refactored constructor `(messages_api_url, config_api_url, api_key, is_browser=False, on_message=None)`; `heart-matrix-controller/main.py` SHALL still import it and SHALL pass the URLs, the `api_key`, and `is_browser=False`; the device's boot path SHALL be functionally equivalent to the previous implementation (REST seed + MQTT subscription + display updates)

### Requirement: /api/live-messages and /api/live-config endpoints are removed

The Flask process SHALL NOT register the routes `GET /api/live-messages`, `POST /api/live-messages/seed`, or `GET /api/live-config`. Requests to these paths SHALL return `404 Not Found`. The `api_live_messages`, `api_live_messages_seed`, and `api_live_config` route handler functions SHALL be deleted from `heart-message-manager/main.py`.

The `/api/messages` (GET and POST) and `/api/config` (GET and PUT) endpoints SHALL remain — the device's `MessageManager.seed()` calls the non-live `/api/messages` and `/api/config` at boot.

#### Scenario: GET /api/live-messages returns 404
- **WHEN** an authenticated client (session cookie, API key, or unauthenticated) requests `GET /api/live-messages`
- **THEN** the Flask process SHALL return `404 Not Found`; no JSON body SHALL be returned

#### Scenario: POST /api/live-messages/seed returns 404
- **WHEN** an authenticated client requests `POST /api/live-messages/seed`
- **THEN** the Flask process SHALL return `404 Not Found`; the seed path SHALL NOT trigger a re-seed of any in-process ring buffer (no such ring exists)

#### Scenario: GET /api/live-config returns 404
- **WHEN** an authenticated client requests `GET /api/live-config`
- **THEN** the Flask process SHALL return `404 Not Found`; no config snapshot SHALL be returned

#### Scenario: GET /api/messages still works for the Pi's boot seed
- **WHEN** the Pi device boots and calls `GET /api/messages` (its `MESSAGES_API_URL`)
- **THEN** the Flask process SHALL return the same JSON shape as before (a list of `Message` dicts ordered by `received_at` descending); the Pi's `MessageManager.seed()` SHALL continue to populate the device's in-memory ring

#### Scenario: GET /api/config still works for the Pi's boot seed
- **WHEN** the Pi device boots and calls `GET /api/config` (its `CONFIG_API_URL`)
- **THEN** the Flask process SHALL return the same `SignConfig` JSON shape as before; the Pi's `MessageManager.seed()` SHALL continue to populate the device's in-memory `SignConfig`

### Requirement: The Twilio ingress, SQLite, and S3 paths are unchanged

The change SHALL NOT modify the `POST /api/messages` Twilio webhook handler, the `PUT /api/config` admin write path, the `POST /api/messages/<id>/suppress` and `POST /api/messages/<id>/unsuppress` routes, the SQLite storage, the S3 backup path, the broker publish envelope, the `/messages`, `/settings`, `/filters`, and `/` admin UI routes, or the `/health` endpoint.

The broker publish — the `mqtt_client.publish_envelope(...)` call in `_save_and_publish` and `_process_inbound_message` — SHALL continue to publish `MessageEnvelope` JSON to the broker topic; the broker delivers to the device and to the browser, neither of which goes through the Flask's ring buffer.

#### Scenario: An incoming Twilio SMS still publishes to the broker
- **WHEN** Twilio posts to `POST /api/messages` and Flask processes the message
- **THEN** the Flask process SHALL publish a `type: "message"` envelope to the broker (via `mqtt_client.publish_envelope`) and SHALL write to SQLite + S3; the device and the browser SHALL each receive the envelope via their own broker subscriptions; the Flask process SHALL NOT have an in-memory ring buffer to update

#### Scenario: An admin config write still publishes to the broker
- **WHEN** the admin UI submits `POST /settings` and Flask processes the change
- **THEN** the Flask process SHALL write the new config to SQLite + S3 and SHALL publish a `type: "config"` envelope to the broker; the device and the browser SHALL each receive the envelope via their own broker subscriptions

### Requirement: The testing page verifies roundtrips via the browser's MQTT-WS client

The `templates/testing.html` page SHALL verify message and config roundtrips using the browser's MQTT-WS client instead of polling the Flask's removed live endpoints. The page SHALL:

- POST a test message to `/api/test-messages` (the existing test ingress path)
- Subscribe to the broker via the in-page `MqttWsClient` (the same client the base template bootstraps)
- When an envelope arrives in the browser's `MessageManager`, the page SHALL update its status block from the in-browser ring buffer
- For config roundtrip verification, the page SHALL trigger a config update via `/api/config` PUT and SHALL observe the corresponding `type: "config"` envelope arrive in the browser's `MessageManager`

The page SHALL NOT call `fetch('/api/live-messages...')`, `fetch('/api/live-messages/seed')`, or `fetch('/api/live-config')`.

#### Scenario: POST test message and observe envelope in browser
- **WHEN** the operator clicks "Send test message" on `/testing` and the browser's MQTT-WS client is connected
- **THEN** the page SHALL POST to `/api/test-messages`; the broker SHALL deliver the `type: "message"` envelope to the browser's WS subscription; the page SHALL observe the envelope in the in-browser `MessageManager.dispatch` path; the page's status block SHALL update with the new body and the "now displaying" indicator SHALL match

#### Scenario: PUT config and observe config envelope in browser
- **WHEN** the operator submits a config change from `/testing` and the browser's MQTT-WS client is connected
- **THEN** the page SHALL PUT to `/api/config`; the broker SHALL deliver the `type: "config"` envelope to the browser's WS subscription; the page SHALL observe the envelope and update its "current config" panel from the in-browser `SignConfig`

#### Scenario: WS not yet ready is surfaced in the page
- **WHEN** the operator opens `/testing` and the browser's MQTT-WS client has not yet reached `connected`
- **THEN** the page SHALL show a "WebSocket connecting…" indicator; the "Send test message" button SHALL be disabled until the WS reaches `connected`; the operator SHALL see a clear error if WS is blocked (corporate proxy, browser extension) with no silent failure

### Requirement: Tests that hit the removed endpoints are replaced with equivalent coverage of browser-internal functions

The change SHALL remove the `/api/live-messages` and `/api/live-config` auth tests from `tests/test_auth.py` (these tests assert behavior of routes that no longer exist) and SHALL delete `tests/preview_poll_test.py` (which asserts the polling URL string). The change SHALL add equivalent coverage of the refactored `MessageManager` and the new browser-internal wrappers in `tests/test_message_manager.py` (refactored class: `is_browser` flag with internal `_fetch`, dispatch, ring buffer, suppression, eviction), `tests/test_mqtt_ws_client_py.py`, and `tests/test_message_buffer_store_py.py`:

- `test_message_manager.py` — `MessageManager` constructor accepts `(messages_api_url, config_api_url, api_key, is_browser)`; `seed()` calls the internal `async def _fetch(self, url)` for both URLs, in order; `_fetch` branches on `is_browser` (server path uses `requests` via `asyncio.to_thread(...)`; browser path uses `js.fetch`); `dispatch` parses valid `message` and `config` envelopes; `dispatch` calls `on_message` only on `message`; `dispatch` respects filter rules; `dispatch` evicts the oldest message at 101 entries; `get_messages(suppress=True)` excludes suppressed
- `test_mqtt_ws_client_py.py` — the `MqttWsClient` Python wrapper is importable and exposes the expected class signature
- `test_message_buffer_store_py.py` — the `MessageBufferStore` Python wrapper is importable and exposes the expected class signature

The auth flow on the remaining endpoints (`/api/messages`, `/api/config`) SHALL continue to be covered by the existing tests in `tests/test_auth.py` (the live-endpoint tests are the only ones removed; the other auth tests stay).

#### Scenario: tests/test_auth.py has no /api/live-messages assertions
- **WHEN** the change is deployed
- **THEN** `tests/test_auth.py` SHALL NOT contain the strings `/api/live-messages` or `/api/live-config`; the auth flow SHALL be covered via the `/api/messages` and `/api/config` test cases that remain

#### Scenario: tests/preview_poll_test.py is deleted
- **WHEN** the change is deployed
- **THEN** the file `tests/preview_poll_test.py` SHALL NOT exist; the polling URL string it asserted is no longer in the codebase

#### Scenario: tests/test_message_manager.py covers the refactored contract
- **WHEN** `PYTHONPATH=. pytest tests/test_message_manager.py -v` is run
- **THEN** the tests SHALL pass: constructor accepts `(messages_api_url, config_api_url, api_key, is_browser)`; `seed()` calls the internal `_fetch` for both URLs, in order (verified by monkey-patching `mgr._fetch`); the server-path branch is verified by patching `_ensure_server_runtime` to return a mock `requests` module; the browser-path branch is verified by patching `_ensure_browser_runtime` to return a mock `js.fetch` callable; valid `message` envelope is dispatched and `on_message` fires; valid `config` envelope updates `SignConfig` and does NOT fire `on_message`; malformed envelope is dropped; filtered message does NOT fire `on_message`; `get_messages(suppress=True)` excludes suppressed; 101st message evicts the oldest
