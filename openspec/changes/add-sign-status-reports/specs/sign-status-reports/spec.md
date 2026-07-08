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
Flask MUST expose a `GET /api/sign-status` endpoint that returns a server-determined state enum plus the latest snapshot in a stable response shape: `{state: "live" | "unsure" | "offline", snapshot: {...} | null, received_at: <iso8601> | null}`. The endpoint MUST always return HTTP 200 OK. The state value MUST be computed server-side from the snapshot's age: `state="live"` when the snapshot was received less than 60 seconds ago, `state="unsure"` when 60-120 seconds ago, `state="offline"` when more than 120 seconds ago or no snapshot has ever been received. When `state` is `"offline"`, the `snapshot` field MUST be `null` and the `received_at` field MUST be `null`. When `state` is `"live"` or `"unsure"`, the `snapshot` field MUST contain the deserialized snapshot and the `received_at` field MUST contain the ISO 8601 timestamp Flask received it.

#### Scenario: Fresh snapshot returns live state
- **WHEN** a snapshot was received 30 seconds ago
- **THEN** `GET /api/sign-status` returns HTTP 200
- **AND** the response body's `state` field is `"live"`
- **AND** the response body's `snapshot` field is the deserialized snapshot
- **AND** the response body's `received_at` field is a valid ISO 8601 timestamp

#### Scenario: Stale-but-recent snapshot returns unsure state
- **WHEN** a snapshot was received 90 seconds ago
- **THEN** `GET /api/sign-status` returns HTTP 200
- **AND** the response body's `state` field is `"unsure"`
- **AND** the response body's `snapshot` field is the deserialized snapshot

#### Scenario: No snapshot has ever been received
- **WHEN** Flask has just started and no snapshot has arrived
- **THEN** `GET /api/sign-status` returns HTTP 200
- **AND** the response body's `state` field is `"offline"`
- **AND** the response body's `snapshot` field is `null`
- **AND** the response body's `received_at` field is `null`

#### Scenario: Snapshot is older than 120 seconds
- **WHEN** the last snapshot was received 180 seconds ago
- **THEN** `GET /api/sign-status` returns HTTP 200
- **AND** the response body's `state` field is `"offline"`
- **AND** the response body's `snapshot` field is `null`
- **AND** the response body's `received_at` field is `null`

#### Scenario: State transitions as snapshot ages
- **WHEN** the most recent snapshot was received 30 seconds ago
- **THEN** `GET /api/sign-status` returns `state="live"`
- **AND WHEN** 60 seconds pass without a new snapshot
- **THEN** `GET /api/sign-status` returns `state="unsure"`
- **AND WHEN** a further 60 seconds pass without a new snapshot
- **THEN** `GET /api/sign-status` returns `state="offline"`

### Requirement: Dashboard "Live" pill reflects sign health
The Dashboard page MUST render a "Live" pill whose color, animation, and text are determined by the `state` field returned by `GET /api/sign-status`. The pill MUST apply the green color and pulse animation and the text "Live" when `state="live"`. The pill MUST apply the amber color and no animation and the text "Live" when `state="unsure"`. The pill MUST apply the grey color and no animation and the text "Unknown" when `state="offline"`. The threshold values for the state transitions are server-side policy; the browser MUST NOT compute thresholds locally.

#### Scenario: Live state shows green pill with pulse
- **WHEN** `GET /api/sign-status` returns `state="live"`
- **THEN** the Dashboard "Live" pill has the green color class applied
- **AND** the pill has the pulse animation class applied
- **AND** the pill text reads "Live"

#### Scenario: Unsure state shows amber pill without pulse
- **WHEN** `GET /api/sign-status` returns `state="unsure"`
- **THEN** the Dashboard "Live" pill has the amber color class applied
- **AND** the pill does NOT have the pulse animation class applied
- **AND** the pill text reads "Live"

#### Scenario: Offline state shows grey "Unknown" pill
- **WHEN** `GET /api/sign-status` returns `state="offline"`
- **THEN** the Dashboard "Live" pill has the grey color class applied
- **AND** the pill text reads "Unknown"

#### Scenario: Browser polls the endpoint every 10 seconds
- **WHEN** the Dashboard page is loaded
- **THEN** the browser calls `GET /api/sign-status` immediately on load
- **AND** the browser calls `GET /api/sign-status` every 10 seconds thereafter
- **AND** each response updates the pill's color, animation, and text within 100ms of receiving the response

#### Scenario: Browser does not compute thresholds locally
- **WHEN** the browser receives a response with `state="unsure"`
- **THEN** the browser renders the amber pill based on the `state` value alone
- **AND** the browser does NOT compute its own age threshold from `received_at` to decide the pill's color

### Requirement: Settings page exposes a read-only Sign Health section
The Settings page MUST render a new read-only "Sign Health" section at the top of the page that displays the snapshot fields: `active_sha`, `started_at`, `uptime_seconds` (formatted as `Xd Yh Zm`), `mqtt_connected`, `last_tick_age_ms`, `messages_rendered`, `last_error`, and the timestamp Flask received the snapshot. The values MUST update in place when the browser polls `GET /api/sign-status`. The section MUST show a "No status received yet" placeholder when `state="offline"`. The section MUST NOT include any form controls — it is read-only. The visibility of the snapshot fields MUST be driven by the server-returned `state`: when `state="offline"`, the field slots are hidden and only the placeholder is shown.

#### Scenario: Snapshot fields render on Settings page when state is live
- **WHEN** `GET /api/sign-status` returns `state="live"` with a non-null snapshot
- **THEN** the Settings page shows the running SHA, started_at timestamp, uptime (formatted), MQTT-connected flag, last-tick age, messages-rendered count, and last-error value
- **AND** the section shows the Flask-side timestamp of when the snapshot was received

#### Scenario: Snapshot fields render when state is unsure
- **WHEN** `GET /api/sign-status` returns `state="unsure"` with a non-null snapshot
- **THEN** the Settings page shows the snapshot fields and a small "stale" indicator next to the Flask-side timestamp

#### Scenario: No snapshot yet shows placeholder
- **WHEN** `GET /api/sign-status` returns `state="offline"` with `snapshot=null`
- **THEN** the Settings page Sign Health section shows the text "No status received yet"
- **AND** the field slots are hidden

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