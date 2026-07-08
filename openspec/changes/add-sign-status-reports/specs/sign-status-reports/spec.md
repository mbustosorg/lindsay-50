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

### Requirement: Flask server subscribes to the status topic and keeps the latest snapshot
The Flask server MUST subscribe to `MQTT_STATUS_TOPIC` (in addition to its existing `MQTT_TOPIC` subscription). For each payload received on the status topic, Flask MUST JSON-parse the payload, validate that the required keys are present (`schema_version`, `active_sha`, `started_at`, `updated_at`, `uptime_seconds`, `mqtt_connected`, `last_tick_age_ms`, `messages_rendered`, `last_error`), and store the decoded payload in an in-memory `LatestSignStatus` store guarded by `threading.RLock`. A malformed payload or a payload missing required keys MUST be logged at WARN level and MUST NOT replace the in-memory snapshot. Flask MUST keep only the most recent snapshot; historical snapshots are not retained. The Flask subscription to `MQTT_STATUS_TOPIC` is independent of the envelope subscription — a status-subscribe failure MUST NOT affect the envelope subscription and vice versa.

#### Scenario: Flask receives and stores a valid snapshot
- **WHEN** a JSON payload with the required keys arrives on `MQTT_STATUS_TOPIC`
- **THEN** the in-memory `LatestSignStatus` store is updated with the decoded payload
- **AND** the store's snapshot is a defensive copy of the decoded payload (mutating the snapshot in place does not affect the store)

#### Scenario: Flask drops a malformed payload
- **WHEN** a payload arrives on `MQTT_STATUS_TOPIC` that is not valid JSON
- **THEN** the in-memory snapshot is NOT replaced
- **AND** a warning is logged at WARN level with the topic and a one-line parse error

#### Scenario: Flask drops a payload missing required keys
- **WHEN** a JSON payload arrives that is missing one of the required keys (e.g., `active_sha`)
- **THEN** the in-memory snapshot is NOT replaced
- **AND** a warning is logged at WARN level naming the missing key

#### Scenario: Flask retains only the most recent snapshot
- **WHEN** three valid snapshots arrive within 10 seconds
- **THEN** the in-memory store contains the third snapshot
- **AND** the store's `snapshot()` method returns the third snapshot, never the first or second

#### Scenario: Status subscription is independent of envelope subscription
- **WHEN** the status subscription's `on_message` callback raises an exception
- **THEN** the envelope subscription continues to receive and dispatch messages
- **AND** the next status message is processed normally on arrival

### Requirement: Flask exposes the latest snapshot via GET /api/sign-status
Flask MUST expose `GET /api/sign-status` returning the latest snapshot from the in-memory `LatestSignStatus` store. The endpoint MUST always return HTTP 200. The response body MUST be a JSON object with the shape `{snapshot: <snapshot_dict> | null, received_at: <iso8601> | null}`. When no snapshot has been received since Flask started, `snapshot` and `received_at` MUST both be `null`. When a snapshot has been received, `received_at` MUST be the ISO-8601 wall-clock timestamp at which Flask stored the snapshot (the value Flask uses internally for age tracking). The endpoint MUST NOT compute or return a derived state value — state computation is browser-side. The endpoint MUST NOT log a warning when the store is empty — a `null` snapshot is an expected response, not an error.

#### Scenario: Endpoint returns stored snapshot
- **WHEN** Flask has received at least one snapshot
- **THEN** `GET /api/sign-status` returns HTTP 200 with `snapshot` equal to the most recent decoded payload
- **AND** `received_at` is an ISO-8601 string

#### Scenario: Endpoint returns null snapshot on empty store
- **WHEN** Flask has not received any snapshot since startup
- **THEN** `GET /api/sign-status` returns HTTP 200 with `snapshot: null`
- **AND** `received_at: null`

#### Scenario: Endpoint never returns 404 or 5xx
- **WHEN** the Flask app is running (regardless of subscription state)
- **THEN** every call to `GET /api/sign-status` returns HTTP 200

#### Scenario: Endpoint does not compute state
- **WHEN** the endpoint serializes the response
- **THEN** the response body MUST NOT contain a `state` key
- **AND** the response body MUST NOT contain any computed field derived from the snapshot's age

### Requirement: Browser hydrates on page load with a one-shot fetch
The browser MUST issue exactly one `fetch('/api/sign-status')` call when an authenticated page (Dashboard or Settings) loads. The fetch is **load-time hydration**: the browser MUST NOT issue any subsequent `fetch` calls against `/api/sign-status` or any other sign-status URL on a timer or in response to MQTT-WS messages. The load-time fetch MUST populate the in-memory browser snapshot if it returns a non-null payload whose `updated_at` is newer than the most recent WS-received snapshot. If the fetch returns a payload with an older `updated_at` than the most recent WS-received snapshot, the browser MUST ignore the fetch response (the WS subscription's fresher data wins). If the fetch returns `{snapshot: null}`, the browser MUST NOT replace any in-memory snapshot — it leaves the existing snapshot alone (or leaves the empty state if no WS message has arrived yet).

#### Scenario: Page load fetches once
- **WHEN** the Dashboard or Settings page loads
- **THEN** the browser issues exactly one `fetch('/api/sign-status')` call
- **AND** the browser does NOT issue any further `fetch('/api/sign-status')` calls during the lifetime of the page

#### Scenario: Fetch response populates empty state
- **WHEN** the page loads and no WS message has arrived
- **AND** the fetch returns a snapshot with `updated_at = T`
- **THEN** the in-memory browser snapshot is the fetched payload

#### Scenario: Fetch response ignored when WS data is fresher
- **WHEN** the page loads and the WS subscription has already received a snapshot with `updated_at = T+10`
- **AND** the fetch returns a snapshot with `updated_at = T`
- **THEN** the in-memory browser snapshot remains the WS-received snapshot
- **AND** the fetch response is dropped

#### Scenario: Fetch null does not clobber empty state
- **WHEN** the page loads and no WS message has arrived
- **AND** the fetch returns `{snapshot: null, received_at: null}`
- **THEN** the in-memory browser snapshot is `null`
- **AND** the UI shows the offline placeholder

### Requirement: Browser subscribes to the status topic via a dedicated MQTT-WS client
The browser MUST subscribe to `MQTT_STATUS_TOPIC` via a second `createMqttWsClient` instance scoped to the status topic only. The existing envelope-flow `createMqttWsClient` instance (subscribed to `MQTT_TOPIC`) MUST remain subscribed to `MQTT_TOPIC` and MUST NOT be changed. The status-WS client MUST have its own reconnect logic and its own status indicator; a status-WS disconnect MUST NOT affect the envelope-WS connection and vice versa. The status-WS client MUST surface its connection state (connected / reconnecting / paused / error) to a small indicator parallel to the existing `#mqtt-status` pattern.

#### Scenario: Status page subscribes on load
- **WHEN** an authenticated page (Dashboard or Settings) loads
- **THEN** the browser opens a WebSocket to the configured `mqttWsUrl` and subscribes to `MQTT_STATUS_TOPIC`
- **AND** the existing envelope-flow WebSocket to `MQTT_TOPIC` is unaffected (independent connection)

#### Scenario: Status-WS disconnect does not affect envelope-WS
- **WHEN** the status-WS connection is dropped
- **THEN** the status-WS client enters its reconnect loop
- **AND** the envelope-WS connection continues to receive `MQTT_TOPIC` messages without interruption

#### Scenario: Envelope-WS disconnect does not affect status-WS
- **WHEN** the envelope-WS connection is dropped
- **THEN** the envelope-WS client enters its reconnect loop
- **AND** the status-WS connection continues to receive `MQTT_STATUS_TOPIC` messages without interruption

### Requirement: Browser decodes each status message into a snapshot
For each payload received on `MQTT_STATUS_TOPIC`, the browser MUST decode the payload as a UTF-8 JSON object and store it as the latest snapshot. The browser MUST validate that the payload contains the required keys (`schema_version`, `active_sha`, `started_at`, `updated_at`, `uptime_seconds`, `mqtt_connected`, `last_tick_age_ms`, `messages_rendered`, `last_error`); a payload missing required keys or not valid JSON MUST be logged at WARN level and MUST NOT replace the in-memory snapshot. The browser MUST keep only the most recent snapshot; historical snapshots are not retained.

#### Scenario: Browser receives and stores a valid snapshot
- **WHEN** a JSON payload with the required keys arrives on `MQTT_STATUS_TOPIC`
- **THEN** the in-memory snapshot is replaced with the decoded payload
- **AND** the Dashboard pill and Settings-page Sign Health section re-render to reflect the new snapshot

#### Scenario: Browser drops a malformed payload
- **WHEN** a payload arrives on `MQTT_STATUS_TOPIC` that is not valid JSON
- **THEN** the in-memory snapshot is NOT replaced
- **AND** a warning is logged at WARN level with the topic and a one-line parse error

#### Scenario: Browser drops a payload missing required keys
- **WHEN** a JSON payload arrives that is missing one of the required keys (e.g., `active_sha`)
- **THEN** the in-memory snapshot is NOT replaced
- **AND** a warning is logged at WARN level naming the missing key

#### Scenario: Browser retains only the most recent snapshot
- **WHEN** three valid snapshots arrive within 10 seconds
- **THEN** the in-memory snapshot contains the third snapshot
- **AND** a re-render sees only the third snapshot, never the first or second

### Requirement: Browser computes sign state from snapshot age
The browser MUST compute the sign's state as one of `"live"`, `"unsure"`, or `"offline"` from `(now - snapshot.updated_at)`, where `now` is the browser's wall clock at the time of computation. The thresholds MUST be: `state="live"` when the snapshot is less than 60 seconds old; `state="unsure"` when 60 to 120 seconds old; `state="offline"` when more than 120 seconds old, OR when no snapshot has ever been received. The threshold constants MUST be named (`LIVE_THRESHOLD_S = 60`, `UNSURE_THRESHOLD_S = 120`) and exported in `sign_status.js` so they are easy to find and tune. The server MUST NOT be involved in computing the state — there is no server-side state field and no HTTP endpoint that returns a computed state value.

#### Scenario: Fresh snapshot yields live state
- **WHEN** the most recent snapshot's `updated_at` is 30 seconds in the past
- **THEN** the computed state is `"live"`

#### Scenario: Stale-but-recent snapshot yields unsure state
- **WHEN** the most recent snapshot's `updated_at` is 90 seconds in the past
- **THEN** the computed state is `"unsure"`

#### Scenario: Old snapshot yields offline state
- **WHEN** the most recent snapshot's `updated_at` is 180 seconds in the past
- **THEN** the computed state is `"offline"`

#### Scenario: No snapshot ever received yields offline state
- **WHEN** the page has loaded and no snapshot has arrived
- **THEN** the computed state is `"offline"`

### Requirement: Browser re-evaluates state on a local timer
The browser MUST run a local `setInterval` (5-second cadence) that re-evaluates the sign state from the in-memory snapshot's age and re-renders the Dashboard pill. The interval MUST produce only DOM updates; it MUST NOT make any network requests (no `fetch`, no new WebSocket connections). The interval MUST be cleared when the page is unloaded. The interval is purely a UI re-render cadence — it does not poll the server.

#### Scenario: Pill transitions from live to unsure without a new message
- **WHEN** the most recent snapshot was received 30 seconds ago (state was `"live"`)
- **AND** 35 seconds pass without a new snapshot arriving
- **THEN** the next interval tick computes state as `"unsure"`
- **AND** the Dashboard pill re-renders to the amber style

#### Scenario: Pill transitions from unsure to offline without a new message
- **WHEN** the most recent snapshot was received 90 seconds ago (state was `"unsure"`)
- **AND** 35 seconds pass without a new snapshot arriving
- **THEN** the next interval tick computes state as `"offline"`
- **AND** the Dashboard pill re-renders to the grey "Unknown" style

#### Scenario: Interval produces no network traffic
- **WHEN** the interval fires
- **THEN** the browser does NOT issue any HTTP requests
- **AND** the browser does NOT open any new WebSocket connections
- **AND** the browser only updates the DOM

#### Scenario: Interval is cleared on page unload
- **WHEN** the user navigates away from the Dashboard or Settings page
- **THEN** the interval is cleared
- **AND** no further re-renders occur after navigation

### Requirement: Dashboard "Live" pill reflects the computed state
The Dashboard page MUST render a "Live" pill whose color, animation, and text reflect the browser-computed state (derived from the snapshot's age — see "Browser computes sign state from snapshot age"). The pill MUST apply the green color and pulse animation and the text "Live" when `state="live"`. The pill MUST apply the amber color and no animation and the text "Live" when `state="unsure"`. The pill MUST apply the grey color and no animation and the text "Unknown" when `state="offline"`. The threshold values for the state transitions are browser-side policy (defined in `sign_status.js`); there is no HTTP endpoint or server round-trip involved in computing or rendering the state.

#### Scenario: Live state shows green pill with pulse
- **WHEN** the computed state is `"live"`
- **THEN** the Dashboard "Live" pill has the green color class applied
- **AND** the pill has the pulse animation class applied
- **AND** the pill text reads "Live"

#### Scenario: Unsure state shows amber pill without pulse
- **WHEN** the computed state is `"unsure"`
- **THEN** the Dashboard "Live" pill has the amber color class applied
- **AND** the pill does NOT have the pulse animation class applied
- **AND** the pill text reads "Live"

#### Scenario: Offline state shows grey "Unknown" pill
- **WHEN** the computed state is `"offline"`
- **THEN** the Dashboard "Live" pill has the grey color class applied
- **AND** the pill text reads "Unknown"

#### Scenario: Pill updates on each new snapshot
- **WHEN** a new snapshot arrives on `MQTT_STATUS_TOPIC`
- **THEN** the Dashboard pill re-renders within 100ms of the message arriving
- **AND** the color, animation, and text reflect the new computed state

#### Scenario: Pill updates on each interval tick
- **WHEN** the 5-second interval fires
- **THEN** the Dashboard pill re-renders to reflect the current computed state
- **AND** no network requests are issued

### Requirement: Settings page exposes a read-only Sign Health section
The Settings page MUST render a new read-only "Sign Health" section at the top of the page that displays the snapshot fields: `active_sha`, `started_at`, `uptime_seconds` (formatted as `Xd Yh Zm`), `mqtt_connected`, `last_tick_age_ms`, `messages_rendered`, `last_error`, and the timestamp the browser received the snapshot. The values MUST update in place when a new snapshot arrives (via the WS subscription) or when the load-time fetch populates the in-memory snapshot. The section MUST show a "No status received yet" placeholder when no snapshot has been received. The section MUST NOT include any form controls — it is read-only. The visibility of the snapshot fields MUST be driven by the computed state: when `state="offline"`, the field slots are hidden and only the placeholder is shown.

#### Scenario: Snapshot fields render on Settings page when state is live
- **WHEN** the computed state is `"live"` and a snapshot is in memory
- **THEN** the Settings page shows the running SHA, started_at timestamp, uptime (formatted), MQTT-connected flag, last-tick age, messages-rendered count, and last-error value
- **AND** the section shows the browser-side timestamp of when the snapshot was received

#### Scenario: Snapshot fields render when state is unsure
- **WHEN** the computed state is `"unsure"` and a snapshot is in memory
- **THEN** the Settings page shows the snapshot fields and a small "stale" indicator next to the received-at timestamp

#### Scenario: No snapshot yet shows placeholder
- **WHEN** the computed state is `"offline"` and no snapshot is in memory
- **THEN** the Settings page Sign Health section shows the text "No status received yet"
- **AND** the field slots are hidden

#### Scenario: Snapshot updates in place without page reload
- **WHEN** a new snapshot is received on `MQTT_STATUS_TOPIC`
- **THEN** the Sign Health section's field values are replaced in place
- **AND** the page does not navigate or reload

### Requirement: MQTT_STATUS_TOPIC is configurable via settings.toml and env
The status topic MUST be configurable via `MQTT_STATUS_TOPIC` in both `heart-message-manager/settings.toml` and `heart-matrix-controller/settings.toml`. Environment variables MUST take precedence over `settings.toml` values, matching the existing config pattern. When `MQTT_STATUS_TOPIC` is empty or unset, the device and server MUST derive it from `MQTT_TOPIC` by appending `-status` (so `mbustosorg/feeds/lindsay-50` becomes `mbustosorg/feeds/lindsay-50-status`). Both `settings.toml.example` files MUST document the new key. The Flask app MUST expose the resolved `MQTT_STATUS_TOPIC` to the browser via the existing `window.APP_CONFIG.mqttStatusTopic` field, alongside the existing `mqttTopic` and `mqttWsUrl`.

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

#### Scenario: Browser receives the resolved topic via APP_CONFIG
- **WHEN** the Flask app serves any page with the `window.APP_CONFIG` block
- **THEN** `window.APP_CONFIG.mqttStatusTopic` contains the resolved status topic
- **AND** `sign_status.js` reads it from `window.APP_CONFIG.mqttStatusTopic` (not from a hardcoded value)

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