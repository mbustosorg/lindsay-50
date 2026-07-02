## ADDED Requirements

### Requirement: Flask exposes the expected version
Flask MUST expose `GET /api/sign/expected-sha` returning `{"expected_sha": "<sha>"}` where `<sha>` is `HEROKU_SLUG_COMMIT` if set, or the local `git rev-parse HEAD` if not (local dev fallback). The endpoint MUST require authentication via the existing `X-API-Key` header.

#### Scenario: Heroku deploy (slug commit set)
- **WHEN** Flask runs on Heroku with `HEROKU_SLUG_COMMIT=abc123`
- **THEN** `GET /api/sign/expected-sha` returns `{"expected_sha": "abc123"}`

#### Scenario: Local dev (no slug commit env var)
- **WHEN** Flask runs locally without `HEROKU_SLUG_COMMIT` set
- **THEN** `GET /api/sign/expected-sha` returns `{"expected_sha": <local git HEAD SHA>}`

#### Scenario: Missing or invalid API key
- **WHEN** request to `/api/sign/expected-sha` has no `X-API-Key` or an invalid one
- **THEN** endpoint returns 401

### Requirement: Pi queries the expected SHA on every boot
The Pi's loader MUST call `GET /api/sign/expected-sha` on every boot before deciding whether to stage a new version. The query MUST use the existing `X-API-Key` from `settings.toml`.

#### Scenario: Boot against matching version
- **WHEN** Pi boots and `/api/sign/expected-sha` returns the same SHA as local HEAD
- **THEN** loader skips upgrade and execs the existing `current/.../main.py`

#### Scenario: Boot against newer version
- **WHEN** Pi boots and `/api/sign/expected-sha` returns a different SHA than local HEAD
- **THEN** loader stages the expected SHA via worktree, runs health check, swaps if healthy

#### Scenario: Boot with Flask unreachable
- **WHEN** Pi boots and `/api/sign/expected-sha` request fails (timeout, connection refused, 5xx)
- **THEN** loader logs the error and execs the existing `current/.../main.py` without attempting upgrade

### Requirement: Flask publishes a reboot command on startup
Flask MUST publish a single `{"type":"command","payload":{"action":"reboot"}}` envelope on the configured MQTT topic immediately after the paho client finishes its initial connection. The publish MUST use the existing `publish_envelope()` path so QoS 1 + PUBACK semantics are preserved.

#### Scenario: First Flask start
- **WHEN** Flask starts and the MQTT client connects for the first time
- **THEN** one `command=reboot` envelope is published on `cfg.MQTT_TOPIC`

#### Scenario: Flask restart (deploy, dyno cycle, manual restart)
- **WHEN** Flask restarts and the MQTT client reconnects
- **THEN** one `command=reboot` envelope is published on `cfg.MQTT_TOPIC` after the reconnection completes

#### Scenario: Pi is offline when Flask publishes
- **WHEN** Flask publishes the reboot command but the Pi is offline or disconnected from the broker
- **THEN** Flask's publish still completes (QoS 1 PUBACK) and the Pi receives the command on its next broker reconnect