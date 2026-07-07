## ADDED Requirements

### Requirement: Pi publishes StatusSnapshot over MQTT on a dedicated status topic
The Pi MUST publish a serialized `StatusSnapshot` to a dedicated MQTT status topic at a wall-clock cadence of 30 seconds (±5 seconds). The publish MUST use QoS 0 (fire-and-forget) so a slow broker cannot stall the render loop. The publish MUST NOT block the render loop: if the publish blocks for more than 5 seconds, the next tick of the render loop MUST still proceed on schedule. The wire payload MUST be a JSON object with the following keys: `schema_version`, `active_sha`, `started_at`, `updated_at`, `uptime_seconds`, `mqtt_connected`, `last_tick_age_ms`, `messages_rendered`, `last_error`. The `pid` field MUST NOT appear in the wire payload.

#### Scenario: Pi publishes on the 30-second cadence
- **WHEN** 30 seconds have elapsed since the previous status publish
- **THEN** the Pi publishes a fresh `StatusSnapshot` JSON payload to the configured `MQTT_STATUS_TOPIC` at QoS 0
- **AND** the payload contains the keys `schema_version`, `active_sha`, `started_at`, `updated_at`, `uptime_seconds`, `mqtt_connected`, `last_tick_age_ms`, `messages_rendered`, `last_error`
- **AND** the payload does NOT contain a `pid` key

#### Scenario: Publish continues when broker is unreachable
- **WHEN** the broker is unreachable and a status publish attempt fails
- **THEN** the next status publish is scheduled for 30 seconds after the failed attempt
- **AND** the render loop's tick interval is unaffected (the publish runs in a separate thread)

#### Scenario: Publish does not block the render loop
- **WHEN** the broker accepts the publish slowly (longer than 100ms but less than 5 seconds)
- **THEN** the next render-loop tick fires on its normal cadence (no perceptible delay)

### Requirement: Flask subscribes to the status topic and keeps the latest snapshot
Flask MUST subscribe to the status topic and keep the most recently received `StatusSnapshot` in an in-memory store guarded by a `threading.RLock`. The store MUST be updated atomically: a snapshot received while a reader is reading MUST NOT cause a torn read. The store MUST retain only the most recent snapshot (no historical buffer). On startup, the store MUST be empty (no snapshot has been received yet).

#### Scenario: Flask receives a snapshot and stores it
- **WHEN** the MQTT subscriber receives a JSON payload on `MQTT_STATUS_TOPIC`
- **THEN** the in-memory store is updated with the deserialized snapshot under the lock
- **AND** a subsequent read returns a snapshot whose `updated_at` matches the received payload

#### Scenario: A stale or unreadable payload is logged and dropped
- **WHEN** the MQTT subscriber receives a payload that is not valid JSON or is missing required keys
- **THEN** the in-memory store is NOT updated
- **AND** a warning is logged at WARN level with the broker topic and a one-line parse error

#### Scenario: Multiple rapid snapshots retain only the most recent
- **WHEN** three snapshots are received within 1 second
- **THEN** the in-memory store contains the third snapshot
- **AND** a reader sees only the third snapshot, never the first or second

### Requirement: Flask exposes GET /api/sign-status
Flask MUST expose a `GET /api/sign-status` endpoint that returns the most recent snapshot received within the last 120 seconds. If no snapshot has been received within 120 seconds (or no snapshot has ever been received), the endpoint MUST return HTTP 204 No Content. If a fresh snapshot is available, the endpoint MUST return HTTP 200 OK with the snapshot as a JSON object matching the wire shape.

#### Scenario: Fresh snapshot is available
- **WHEN** a snapshot was received 30 seconds ago
- **THEN** `GET /api/sign-status` returns HTTP 200
- **AND** the response body is a JSON object containing the keys `active_sha`, `started_at`, `updated_at`, `uptime_seconds`, `mqtt_connected`, `last_tick_age_ms`, `messages_rendered`, `last_error`, `received_at_flask`

#### Scenario: No snapshot has ever been received
- **WHEN** Flask has just started and no snapshot has arrived
- **THEN** `GET /api/sign-status` returns HTTP 204
- **AND** the response body is empty

#### Scenario: Snapshot is older than 120 seconds
- **WHEN** the last snapshot was received 180 seconds ago
- **THEN** `GET /api/sign-status` returns HTTP 204
- **AND** the response body is empty

### Requirement: Dashboard "Live" pill reflects sign health
The Dashboard page MUST render a "Live" pill whose color and pulse animation reflect the age of the latest snapshot. The pill MUST be green with a pulse animation when the snapshot is less than 60 seconds old. The pill MUST be amber (no pulse) when the snapshot is between 60 and 120 seconds old. The pill MUST be grey with the text "Unknown" when no snapshot has been received within 120 seconds or no snapshot has ever been received.

#### Scenario: Fresh snapshot shows green pill
- **WHEN** a snapshot was received 30 seconds ago
- **THEN** the Dashboard "Live" pill is rendered with a green background and a pulse animation
- **AND** the pill text reads "Live"

#### Scenario: Stale snapshot shows amber pill
- **WHEN** a snapshot was received 90 seconds ago
- **THEN** the Dashboard "Live" pill is rendered with an amber background and no pulse animation
- **AND** the pill text reads "Live"

#### Scenario: No recent snapshot shows grey "Unknown" pill
- **WHEN** no snapshot has been received within the last 120 seconds
- **THEN** the Dashboard "Live" pill is rendered with a grey background
- **AND** the pill text reads "Unknown"

#### Scenario: Browser polls the endpoint every 10 seconds
- **WHEN** the Dashboard page is loaded
- **THEN** the browser calls `GET /api/sign-status` immediately on load
- **AND** the browser calls `GET /api/sign-status` every 10 seconds thereafter
- **AND** each response updates the pill's color, animation, and text within 100ms of receiving the response

### Requirement: Settings page exposes a read-only Sign Health section
The Settings page MUST render a new read-only "Sign Health" section at the top of the page that displays the snapshot fields: `active_sha`, `started_at`, `uptime_seconds` (formatted as `Xd Yh Zm`), `mqtt_connected`, `last_tick_age_ms`, `messages_rendered`, `last_error`, and the timestamp Flask received the snapshot. The values MUST update in place when the browser polls `GET /api/sign-status`. The section MUST show a "No status received yet" placeholder when no snapshot has arrived. The section MUST NOT include any form controls — it is read-only.

#### Scenario: Snapshot fields render on Settings page
- **WHEN** a fresh snapshot is available
- **THEN** the Settings page shows the running SHA, started_at timestamp, uptime (formatted), MQTT-connected flag, last-tick age, messages-rendered count, and last-error value
- **AND** the section shows the Flask-side timestamp of when the snapshot was received

#### Scenario: No snapshot yet shows placeholder
- **WHEN** no snapshot has been received since Flask started
- **THEN** the Settings page Sign Health section shows the text "No status received yet"
- **AND** the field slots remain empty

#### Scenario: Snapshot updates in place without page reload
- **WHEN** a new snapshot is received by the browser poll
- **THEN** the Sign Health section's field values are replaced in place
- **AND** the page does not navigate or reload

### Requirement: MQTT_STATUS_TOPIC is configurable via settings.toml and env
The status topic MUST be configurable via `MQTT_STATUS_TOPIC` in both `heart-message-manager/settings.toml` and `heart-matrix-controller/settings.toml`. Environment variables MUST take precedence over `settings.toml` values, matching the existing config pattern. When `MQTT_STATUS_TOPIC` is empty or unset, the device and server MUST derive it from `MQTT_TOPIC` by appending `-status` (so `mbustosorg/feeds/lindsay-50` becomes `mbustosorg/feeds/lindsay-50-status`). Both `settings.toml.example` files MUST document the new key.

#### Scenario: Empty MQTT_STATUS_TOPIC derives from MQTT_TOPIC
- **WHEN** `MQTT_TOPIC` is `mbustosorg/feeds/lindsay-50` and `MQTT_STATUS_TOPIC` is empty
- **THEN** the resolved status topic is `mbustosorg/feeds/lindsay-50-status`

#### Scenario: Explicit MQTT_STATUS_TOPIC overrides the derived value
- **WHEN** `MQTT_STATUS_TOPIC` is set to `custom/path`
- **THEN** the resolved status topic is `custom/path`

#### Scenario: Environment variable takes precedence over settings.toml
- **WHEN** `MQTT_STATUS_TOPIC` is set in the environment to `env/path`
- **AND** `settings.toml` has `MQTT_STATUS_TOPIC = "toml/path"`
- **THEN** the resolved status topic is `env/path`

### Requirement: Status publish does not regress .status.json or envelope publish
The existing `.status.json` write cadence (3-second throttle, atomic `os.replace`) MUST remain unchanged. The existing `MessageEnvelope` publish path on `MQTT_TOPIC` MUST remain unchanged. A status publish failure MUST NOT prevent subsequent envelope publishes or `.status.json` writes. The status publish MUST be on a separate MQTT client invocation per call (matching the existing `publish_envelope` pattern), and MUST NOT share a long-lived publisher with the envelope path.

#### Scenario: Status publish failure does not affect envelope path
- **WHEN** a status publish fails (broker unreachable)
- **THEN** the next `MessageEnvelope` publish on `MQTT_TOPIC` proceeds on its normal cadence
- **AND** the next `.status.json` write proceeds on its normal cadence

#### Scenario: Status publish does not affect the render loop
- **WHEN** a status publish takes 1 second to complete
- **THEN** the render loop's tick interval is unchanged
- **AND** the `.status.json` write (on the render loop) still fires every 3 seconds