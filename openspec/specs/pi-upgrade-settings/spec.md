# pi-upgrade-settings Specification

## Purpose
TBD - created by archiving change add-pi-upgrade-controls. Update Purpose after archive.
## Requirements
### Requirement: Pi Upgrade Control section in Settings UI

The Settings page MUST expose a "Pi Upgrade Control" section with three read-only version displays, one editable input + Clear button, and three command buttons. The section MUST render both on the Bootstrap 5 (`/settings`) and the Tailwind (`/playful/settings`) variants of the Settings page, sharing the same backend data.

The section MUST include the following fields, in order:

- **Flask version** (read-only) — the commit SHA Flask is currently running (short form). Source: Flask's runtime (computed once at startup; cached for the process lifetime). When no Flask status snapshot has been received within the first 5 seconds of the operator opening the page, the field shows "loading…".
- **Pi version** (read-only) — the commit SHA the Pi is currently running (short form). Source: the latest `StatusSnapshot` published by the Pi on `MQTT_STATUS_TOPIC` (established by the merged `add-sign-status-reports` change; consumed by the browser via the existing second `createMqttWsClient` instance and the `GET /api/sign-status` hydration endpoint). When no snapshot has been received, the field MUST display "No status received yet".
- **Target Pi version** (editable input + Clear button) — the SHA the Pi should be running. Empty input is a valid value and means "track Flask version" — Flask resolves this on its side before persisting and publishing (Flask always stores and emits a concrete short SHA, never null). A "Clear" button alongside the input returns the field to the empty state. When the persisted `sign.target_version` is empty, a small hint text below the input MUST read "Tracking Flask version — currently `<flask-sha>`".

Three command buttons MUST render at the bottom of the section:

- **Force upgrade** — non-destructive; requires a `confirm()` dialog before publishing.
- **Restart** — destructive; requires a `confirm()` dialog before publishing.
- **Shutdown** — destructive; requires a `confirm()` dialog before publishing.

In v1 there is no `AUTO_UPDATE` UI control. The Pi-side "Automatically update" knob already lives in `heart-matrix-controller/settings.toml` as `AUTO_UPDATE = true|false` (read by `loader.py:833` — pre-existing); it is flipped by SSH + edit or by the systemd unit `Environment=AUTO_UPDATE=true` env override. This change does not modify that key. A future change will add a Pi-side `settings.json` override file mirroring `effects_settings.json` so the operator can flip the flag from the Pi without redeploying settings.toml (see `Future: Pi-side override file` requirement below).

#### Scenario: New operator visits Settings page for the first time

- **WHEN** operator opens `/settings` and no `StatusSnapshot` has been received within the last 60 seconds
- **THEN** Flask version is populated immediately (Flask knows its own SHA via the merged `add-sign-status-reports` Flask-side publisher — first snapshot ≤5s after Flask boot), Target Pi version input is empty (with the "Tracking Flask version" hint showing Flask's SHA), Pi version displays "No status received yet", three command buttons render in disabled state until Pi status arrives

#### Scenario: Operator pins the Pi to a specific SHA

- **WHEN** operator types a SHA into the Target Pi version input and clicks Save
- **THEN** the new value is saved to Flask on form submit (via the existing `POST /api/config` endpoint, with the new `sign.target_version` field included in the JSON body); Flask validates it is a 1-to-7-character alphanumeric string before persisting; the persisted `SignConfig` row in SQLite reflects the value; Flask publishes a `type=config` envelope on the existing `MQTT_TOPIC` with the updated `sign.target_version`; on the next loader check (or the next config envelope arrival), the Pi treats the pinned SHA as its target

#### Scenario: Operator clears the Target to track Flask version

- **WHEN** operator clicks the Clear button next to the Target Pi version input (or empties it manually) and clicks Save
- **THEN** Flask resolves the empty input to its own running short SHA before persisting; the persisted `SignConfig` row reflects that concrete value; a `type=config` envelope is published with `sign.target_version = "<flask-sha>"`; on the next loader check, the Pi uses Flask's running SHA as its target

### Requirement: `sign.target_version` persists on `SignSettings` (always concrete on the wire)

The `target_version` field MUST live on `SignSettings` in `lib_shared/models.py` as a top-level field with type `str` (always a concrete 1-to-7-character alphanumeric SHA) and a default that resolves to the Flask running SHA (short form) at construction time when not explicitly set. The field MUST round-trip through `to_dict()` / `from_dict()`.

The persistence layer MUST follow the existing SQLite-from-S3 rebuild pattern (`heart-message-manager/sqlite.py`); on S3-rebuild, the `SignSettings.target_version` row MUST re-initialize to Flask's running SHA (short form) at construction time, mirroring how every other `SignSettings` field behaves.

#### Scenario: Flask restarts after operator pinned the Pi to a specific SHA

- **WHEN** operator saved `sign.target_version = "abc123"`, then Flask restarts (config:set, dyno cycle, redeploy)
- **THEN** the Settings page renders with Target Pi version input pre-populated to `abc123`; the persisted SQLite row reflects that value; the `/api/sign/settings` HTTP response returns `target_version: "abc123"`

#### Scenario: S3 rebuild wipes SQLite

- **WHEN** the operator's S3 bucket loses the SQLite snapshot and Flask rebuilds from S3 on next start
- **THEN** the `sign_settings` row is re-created with `target_version` initialized to Flask's running short SHA (the default path; no operator pin survives the S3 wipe); the Settings page renders the input pre-populated with that SHA

### Requirement: Settings ride the existing `type=config` envelope

When the operator saves a change on the Pi Upgrade Control section, Flask MUST publish exactly one envelope on `MQTT_TOPIC` using the **existing** `type=config` shape (the same envelope Flask already publishes when any other `SignConfig` field is saved). The envelope's `payload` MUST contain the full updated `SignConfig` dict, including the new `sign.target_version` field.

The envelope MUST be published via the existing `PahoMqttClient.publish_envelope` path. The payload's `sign.target_version` MUST match the value just persisted to SQLite (already concrete — Flask resolved operator-pin vs Flask-self before persisting).

#### Scenario: Operator clicks Save on the Settings form

- **WHEN** the operator submits the Pi Upgrade Control form with a valid `sign.target_version` value (or empty, which Flask resolves to its own running SHA)
- **THEN** Flask persists the new `SignConfig` to SQLite, returns `200 OK` from `POST /api/config`, and publishes exactly one `type=config` envelope with the full updated `SignConfig` payload

#### Scenario: Operator clicks Save twice in quick succession

- **WHEN** the operator submits two saves within 500 ms
- **THEN** Flask persists both writes (last write wins on SQLite), publishes exactly one envelope per save (the second envelope overwrites the first in the broker), and the Pi handles only the latest payload via the existing `MessageManager._handle_config` path

### Requirement: Flask publishes `type=config` on boot (with skip-if-unchanged)

Flask's startup MUST publish the latest `type=config` envelope exactly once after the persisted `SignConfig` is loaded. To keep MQTT traffic flat on routine deploys that don't touch settings, Flask MUST compute a hash of the `SignConfig.to_dict()` payload and compare it against the hash stored in a sidecar file (e.g. `.last_published_config_hash` in the Flask working dir). When the hashes match, Flask MUST skip the publish. When they differ, Flask MUST publish the envelope and update the sidecar.

On publish failure (broker unreachable, auth error), Flask MUST log a WARN and continue startup — a missed publish means the Pi uses stale settings until the next operator save or Flask restart, which is bounded and recoverable.

This requirement runs in parallel with the existing `check-for-update` one-shot envelope during the transitional period (both are published at startup; the `check-for-update` will be removed in a follow-up change).

#### Scenario: Flask restarts after a routine deploy that did not touch settings

- **WHEN** Flask restarts and the persisted `SignConfig` hash matches the sidecar hash
- **THEN** Flask does NOT publish a `type=config` envelope; the operator sees no spurious MQTT traffic in the broker log; the Pi continues with its in-memory config

#### Scenario: Flask restarts after the operator saved a new `sign.target_version`

- **WHEN** Flask restarts and the persisted `SignConfig` hash differs from the sidecar hash (because `sign.target_version` was updated since the last boot)
- **THEN** Flask publishes exactly one `type=config` envelope with the new `sign.target_version`, updates the sidecar hash, and the Pi's `_handle_config` updates its in-memory `SignConfig`

#### Scenario: Flask boots but the broker is unreachable

- **WHEN** Flask starts and `mqtt_client.publish_envelope` returns False (broker not reachable within the connect timeout)
- **THEN** Flask logs a WARN with the failure detail, does NOT update the sidecar hash, and continues startup — the operator can trigger a republish by saving any Settings field

### Requirement: Future: Pi-side override file for `AUTO_UPDATE`

A future change will add a Pi-side `settings.json` override file (mirroring the existing `effects_settings.json` mechanism) so the operator can flip the `AUTO_UPDATE` flag from the Pi without going through Flask or redeploying settings.toml. This is the recovery path when Flask is unreachable or broken — a deploy that leaves the Pi refusing auto-updates can be unstuck by SSHing in and writing a JSON file.

v1 does NOT include this file. The Pi-side "Automatically update" flag in v1 is settable only via `AUTO_UPDATE = true|false` in `heart-matrix-controller/settings.toml` (or via the systemd unit `Environment=AUTO_UPDATE=true` env override).

#### Scenario: Operator recovers a stuck Pi by writing a settings.json file (future)

- **WHEN** a future change adds the Pi-side override file and the operator writes `{"AUTO_UPDATE": false}` to it via SSH
- **THEN** the loader reads the file on its next boot and uses the override value for `AUTO_UPDATE`, ignoring the settings.toml value

### Requirement: Flask-side status publish on the existing MQTT status topic

Flask MUST publish a status snapshot on `MQTT_STATUS_TOPIC` on a **5-second** cadence (matching the merged `add-sign-status-reports` Pi-side cadence — one cadence constant drives the whole system). The payload MUST be JSON with the full 8-key `StatusSnapshot` shape from `heart-matrix-controller/status.py` (`schema_version`, `active_sha`, `short_sha`, `started_at`, `updated_at`, `uptime_seconds`, `mqtt_connected`, `last_error`) plus a `source` discriminator:

- `source` — the literal string `"flask"` so the browser can disambiguate from Pi-published snapshots (which carry `source: "pi"`).
- `active_sha` — Flask's running commit SHA (`HEROKU_SLUG_COMMIT` or local fallback).
- `started_at` — RFC3339 timestamp of Flask process start.
- `uptime_seconds` — integer seconds since `started_at`.
- `mqtt_connected` — Flask's view of the broker connection (mirror of the publish-result of the last Flask status tick, not the Pi's broker connection).
- `last_error` — last Flask-side publish error message; empty string when healthy.

Publishes MUST use QoS 0 (fire-and-forget) and MUST NOT block on broker response. The publish cadence MUST be independent of any Flask request load.

#### Scenario: Flask boots and the timer publishes the first status

- **WHEN** 5 seconds have passed since `gunicorn main:app` started and the timer has not been rescheduled
- **THEN** Flask publishes a payload on `MQTT_STATUS_TOPIC` matching the 8-key `StatusSnapshot` shape with `source: "flask"`, `started_at` matching Flask boot time, and `uptime_seconds: 5`

#### Scenario: MQTT broker is briefly unavailable

- **WHEN** the broker rejects the connect within the 5-second timeout
- **THEN** the publish attempt returns False, the timer logs the failure, and the next 5-second tick retries — the Flask request loop MUST NOT block on the broker

### Requirement: GET /api/sign/settings returns resolved sign settings

Flask MUST expose `GET /api/sign/settings` returning a JSON object with two fields, both always concrete on the wire:

```json
{
  "target_version": "<7-char short SHA>",
  "timezone": "US/Pacific"
}
```

The endpoint MUST require the same `X-API-Key` auth as `/api/config`. The Pi's loader calls this endpoint on boot; the response is parsed and `target_version` is used as the upgrade target (see `mqtt-command-envelope` spec for the loader-side contract).

`target_version` is resolved server-side: if the persisted `SignSettings.target_version` is operator-pinned (set explicitly), the endpoint returns that value; if empty, Flask resolves to its own running short SHA before responding. The wire form is therefore always a concrete short SHA — the Pi never sees null.

`timezone` is read from `SignConfig.timezone` (top-level; pre-existing) and defaults to `"US/Pacific"`.

This endpoint runs **in parallel** with the existing `GET /api/sign/boot-config` (which legacy Pis continue to call). New Pis call this endpoint; the legacy endpoint is unchanged.

#### Scenario: Loader calls GET /api/sign/settings on boot

- **WHEN** the Pi boots and the persisted `SignSettings.target_version` is `"abc1234"` (operator-pinned)
- **THEN** the endpoint returns `{"target_version": "abc1234", "timezone": "US/Pacific"}`; the loader uses `"abc1234"` as the upgrade target

#### Scenario: Loader calls GET /api/sign/settings when no operator pin is set

- **WHEN** the Pi boots and the persisted `SignSettings.target_version` is empty (no operator pin)
- **THEN** Flask resolves the empty value to its own running short SHA at request time; the endpoint returns `{"target_version": "<flask-sha>", "timezone": "US/Pacific"}`; the loader uses Flask's SHA as the upgrade target (mirrors today's behavior when the operator has not pinned anything)

#### Scenario: GET /api/sign/settings fails (network, HTTP error, missing field)

- **WHEN** the Pi calls the endpoint and the request times out, returns 5xx, returns malformed JSON, or returns no `target_version` field
- **THEN** the loader falls through to running `current/.../main.py` without upgrade (safe default; same failure mode as today's `boot-config`)

#### Scenario: Auth fails on GET /api/sign/settings

- **WHEN** the Pi calls the endpoint with a missing or invalid `X-API-Key`
- **THEN** the endpoint returns `401 Unauthorized`; the loader treats this the same as any other failure (fall through, no upgrade)

