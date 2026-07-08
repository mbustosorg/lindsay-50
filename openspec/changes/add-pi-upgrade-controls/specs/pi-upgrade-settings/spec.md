## ADDED Requirements

### Requirement: Pi Upgrade Control section in Settings UI

The Settings page MUST expose a "Pi Upgrade Control" section with three read-only version displays and two editable controls plus three command buttons. The section MUST render both on the Bootstrap 5 (`/settings`) and the Tailwind (`/playful/settings`) variants of the Settings page, sharing the same backend data.

The section MUST include the following fields, in order:
- **Flask version** (read-only) — the commit SHA Flask is currently running. Source: Flask's runtime (computed once at startup; cached for the process lifetime).
- **Pi version** (read-only) — the commit SHA the Pi is currently running. Source: the latest `StatusSnapshot` published by the Pi on `MQTT_STATUS_TOPIC`. When no snapshot has been received, the field MUST display "No status received yet".
- **Target Pi version** (editable input) — the SHA the Pi should be running. Pre-populated with Flask version on first render.
- **Flask "Automatically update"** (editable checkbox) — when enabled, the Target Pi version input is disabled and tracks Flask version 1:1.
- **Pi "Automatically update"** (read-only checkbox) — populated from the latest `StatusSnapshot.pi_auto_update` field. Always `True` in v1.

Three command buttons MUST render at the bottom of the section:
- **Force upgrade** — non-destructive; no extra confirmation modal beyond the browser's native `confirm()` dialog.
- **Restart** — destructive; requires a `confirm()` dialog before publishing.
- **Shutdown** — destructive; requires a `confirm()` dialog before publishing.

#### Scenario: New operator visits Settings page for the first time
- **WHEN** operator opens `/settings` and no `StatusSnapshot` has been received within the last 60 seconds
- **THEN** Flask version is populated immediately (Flask knows its own SHA), Target Pi version pre-populates to Flask version, Pi version displays "No status received yet", both auto-update checkboxes render (Flask enabled, Pi enabled by default), three command buttons render in disabled state until Pi status arrives

#### Scenario: Operator enables Flask-side auto-update while at the Settings page
- **WHEN** operator clicks the Flask "Automatically update" checkbox to enable it
- **THEN** the Target Pi version input greys out and its value tracks Flask version automatically; the new flag state is saved to Flask on form submit; an MQTT `set-upgrade-settings` envelope is published with `flask_auto_update=true`

#### Scenario: Operator disables Flask-side auto-update and pins a specific version
- **WHEN** operator unchecks Flask "Automatically update" and types a SHA into the Target Pi version input
- **THEN** the input remains editable; the new value is saved to Flask on form submit; an MQTT `set-upgrade-settings` envelope is published with `target_pi_sha=<typed-sha>` and `flask_auto_update=false`

### Requirement: Pi-side auto-update flag is read-only in v1

The Pi-side "Automatically update" checkbox on the Settings page MUST be read-only. The operator MUST NOT be able to toggle it via the UI. The Pi reports its current value through the status flow only; v1 always reports `True`.

#### Scenario: Operator attempts to toggle the Pi-side auto-update checkbox
- **WHEN** operator hovers the Pi-side "Automatically update" checkbox and clicks
- **THEN** the checkbox state does not change; a tooltip or hint text indicates the field is reserved and currently always on

#### Scenario: Pi reports a future auto-update flag value (deferred functionality)
- **WHEN** a future Pi firmware publishes `pi_auto_update=false` in its `StatusSnapshot`
- **THEN** the Settings page displays the unchecked state without allowing click; no Flask action is taken

### Requirement: Persisted Pi upgrade settings survive Flask restarts

A `PiUpgradeSettings` row MUST persist the operator's `target_pi_sha`, `flask_auto_update`, and `pi_auto_update` values in the Flask SQLite DB alongside `SignConfig`. The row MUST be re-created on startup if missing (idempotent schema), with defaults `target_pi_sha = HEROKU_SLUG_COMMIT` (or local `git rev-parse HEAD` fallback), `flask_auto_update = True`, `pi_auto_update = True`.

The persistence layer MUST follow the existing SQLite-from-S3 rebuild pattern (`heart-message-manager/sqlite.py`); on S3-rebuild, the `pi_upgrade_settings` row MUST re-initialize from defaults, mirroring how `SignConfig` behaves.

#### Scenario: Flask restarts after operator changed Target Pi version
- **WHEN** operator saved `target_pi_sha=abc123` and `flask_auto_update=false`, then Flask restarts (config:set, dyno cycle, redeploy)
- **THEN** the Settings page renders with Target Pi version = `abc123` and Flask auto-update unchecked; the persisted SQLite row reflects those values

#### Scenario: S3 rebuild wipes SQLite
- **WHEN** the operator's S3 bucket loses the SQLite snapshot and Flask rebuilds from S3 on next start
- **THEN** the `pi_upgrade_settings` table is re-created with default values (target = current Flask SHA, auto-update flags both True); no migration is required

### Requirement: set-upgrade-settings MQTT envelope

When the operator saves a change on the Pi Upgrade Control section, Flask MUST publish exactly one envelope on `MQTT_TOPIC` with shape:

```json
{
  "type": "command",
  "payload": {
    "action": "set-upgrade-settings",
    "target_pi_sha": "abc123",
    "flask_auto_update": true,
    "pi_auto_update": true
  }
}
```

The envelope MUST be published via the existing `PahoMqttClient.publish_envelope` path. The Payload's three values MUST match the values just persisted to SQLite.

#### Scenario: Operator clicks Save on the Settings form
- **WHEN** the operator submits the Pi Upgrade Control form with valid values
- **THEN** Flask persists the new values to SQLite, returns `200 OK` from the POST endpoint, and publishes exactly one `set-upgrade-settings` envelope with the new payload

#### Scenario: Operator clicks Save twice in quick succession (debounce)
- **WHEN** the operator submits two saves within 500 ms
- **THEN** Flask persists both writes (last write wins on SQLite), publishes exactly one envelope per save (the second envelope overwrites the first in the broker), and the Pi handles only the latest payload

### Requirement: Flask-side status publish on the existing MQTT status topic

Flask MUST publish a status snapshot on `MQTT_STATUS_TOPIC` on a 30-second `threading.Timer` cadence starting 30 seconds after Flask boot. The payload MUST be JSON with at least:

- `active_sha` — Flask's running commit SHA (`HEROKU_SLUG_COMMIT` or local fallback).
- `started_at` — RFC3339 timestamp of Flask process start.
- `uptime_seconds` — integer seconds since `started_at`.
- `source` — the literal string `"flask"` so the browser can disambiguate from Pi-published snapshots.

Publishes MUST use QoS 0 (fire-and-forget) and MUST NOT block on broker response. The publish cadence MUST be independent of any Flask request load.

#### Scenario: Flask boots and the timer publishes the first status
- **WHEN** 30 seconds have passed since `gunicorn main:app` started and the timer has not been rescheduled
- **THEN** Flask publishes a `{"source":"flask","active_sha":"...","started_at":"...","uptime_seconds":30}` payload on `MQTT_STATUS_TOPIC`

#### Scenario: MQTT broker is briefly unavailable
- **WHEN** the broker rejects the connect within the 5-second timeout
- **THEN** the publish attempt returns False, the timer logs the failure, and the next 30-second tick retries — the Flask request loop MUST NOT block on the broker
