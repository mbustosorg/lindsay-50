## Why

Managing Pi upgrades today is binary: either auto-upgrade runs on Flask deploys (`heart-matrix-controller/check_for_update.py` + the loader built in `self-upgrading-matrix-controller`) or the operator SSHes in by hand. There is no way from the Flask admin UI to pin the Pi to a specific version, to override auto-upgrade from the Pi side after a bad deploy, or to issue a force-upgrade / restart / shutdown without physical access. The operator is not always on-site, and SSHing to recover from a failed upgrade is the exact failure mode the original `self-upgrading-matrix-controller` change was trying to eliminate. We need operator-facing controls that ride on top of the existing loader + SHA-exchange mechanism.

Three concrete gaps this change closes:

1. **No version pinning.** If the Pi is healthy at version `abc` but Flask just deployed `def` and the auto-upgrade handler triggers, the operator has no way to keep the Pi at `abc` short of disabling auto-upgrade in code and redeploying.
2. **No override from the Pi side.** If the Pi discovers (via its own startup checks) that the current version is broken in some way `loader.py:probe` cannot detect — e.g. it boots but a render pattern regressed — the operator cannot tell the Pi "stop auto-upgrading, hold this version" without redeploying.
3. **No recovery commands.** Force-upgrade, restart, and shutdown all require SSH today. A force-upgrade is the natural recovery path when the Pi auto-updated to a broken version; restart/shutdown are needed after settings changes that require a reboot (e.g. Wi-Fi reconfiguration).

The status data needed to drive the UI (Pi's running SHA, Flask's running SHA, last tick, MQTT-connected state) is already on the roadmap in `add-sign-status-reports` (Pi→browser MQTT status flow). This change consumes those fields and adds Flask-side status reporting (Flask version + boot-config digest) plus the operator-facing controls.

## What Changes

- **New Settings UI section — "Pi Upgrade Control".** Three read-only fields plus two editable fields with auto-update toggles:
  - **Flask version** — read-only display of the Flask server's currently-running commit SHA. Sourced from Flask's runtime (computed once at startup, cached); surfaced both here and on the Dashboard so the operator always knows what *should* be on the Pi.
  - **Pi version** — read-only display of the Pi's running SHA. Sourced from the latest `StatusSnapshot` consumed via the `add-sign-status-reports` browser→MQTT-WS flow. When no snapshot has been received yet, the field shows a "No status received yet" placeholder (mirrors the existing Sign Health section's offline behavior).
  - **Target Pi version** — editable. Defaults to the Flask version. When the operator changes it (or "Automatically update" is enabled and Flask version changes), the new target is published to the Pi. When "Automatically update" is **enabled**, the field is greyed out and tracks Flask version 1:1; turning it **off** unlocks the field for manual pinning. Changing this field MAY trigger a Pi upgrade if the Pi's "Automatically update" flag is also enabled — that intent is captured in tasks.
  - **Pi "Automatically update"** — editable checkbox. When enabled, the Pi attempts to upgrade to the target version whenever it changes (or on Flask startup, via the existing one-shot hint). When disabled, the Pi only upgrades on explicit `force-upgrade` command. This is the **per-Pi override** that lets the operator stop the auto-upgrade loop without redeploying code.
  - **Flask-side "Automatically update" toggle** — paired with Target Pi version, mirrors the Pi-side flag from Flask's perspective (see "Two flags, one intent" note below).
- **New commands** issued via the existing `type=command` MQTT envelope + `MessageManager.dispatch` handler mechanism (introduced in `self-upgrading-matrix-controller`):
  - **`force-upgrade`** — tells the Pi to attempt an upgrade to the current target version NOW, regardless of auto-update flags. Issued from a button on the new Settings section. The Pi handler `os.execvpe`s into the loader, which performs the standard SHA-check + stage + probe + swap (or no-op if already at target).
  - **`restart`** — reboots the Pi. Issued from a button. The handler calls `os.system("sudo reboot")` (loader already runs as root via systemd). After reboot, the loader runs on startup and uses the current settings to decide whether to upgrade — so a restart is implicitly also "re-check upgrade criteria".
  - **`shutdown`** — powers off the Pi. Issued from a button. The handler calls `os.system("sudo shutdown -h now")`. After power-on, the loader runs and decides whether to upgrade.
  - All three commands are **flask-originated** (Flask publishes, Pi subscribes). They live alongside the existing `check-for-update` handler in `MessageManager.dispatch`. The Pi registers them at startup; Flask publishes them on button click.
- **Flask-side status reporting.** Flask starts reporting its own runtime SHA on the same `MQTT_STATUS_TOPIC` (or a parallel `MQTT_FLASK_STATUS_TOPIC`) so the browser can show what the operator *thinks* is running alongside what the Pi *says* is running. The wire shape mirrors a subset of `StatusSnapshot` (`active_sha`, `started_at`, `uptime_seconds`); the same 30s cadence as the Pi.
- **Settings persistence.** Target Pi version and the two auto-update flags persist on the Flask side (in `SignConfig` or a new sibling model) so they survive Flask restarts. They are NOT on the Pi side — the Pi's "Automatically update" flag is reported back as Pi-side state via the status flow, but the loader does not persist it (it's a UI affordance, not a hard brake; the loader always honors a `force-upgrade` regardless).
- **Two flags, one intent.** The Flask-side "Automatically update" toggle and the Pi-side "Automatically update" toggle are *both* part of this change because they together implement the issue's intent: "control from the Pi side to ignore Target Pi version". The actual upgrade decision is: Pi upgrades IFF (target_sha ≠ active_sha) AND (Pi-auto-update flag is on) AND (Flask-auto-update flag is on, meaning Flask published the new target). Either flag off = hold current version. `force-upgrade` bypasses both flags.

### Out of Scope (parked for follow-up changes)

- A separate MQTT topic for discrete events (per the existing parking lot in `add-sign-status-reports`).
- A PI-side persistent pin (the issue text says "We may add the ability to try to pin it to a specific version in the future, if needed" — deferred).
- Per-SHA rollback UI (the existing `heroku rollback` operator workflow remains the rollback path; this change does not surface it in the admin UI).

## Capabilities

### New Capabilities

- `pi-upgrade-settings`: New admin UI section ("Pi Upgrade Control") with three read-only version fields (Flask, Pi, Target Pi) and two editable fields with auto-update toggles. Target version and auto-update flags persist server-side via `SignConfig` (or new sibling model). Settings changes publish a new `type=upgrade-settings` MQTT envelope so the Pi sees the latest target and flags without polling.
- `pi-power-commands`: New `type=command` actions (`force-upgrade`, `restart`, `shutdown`) routed through `MessageManager.dispatch` to per-action handlers on the Pi. Flask-side buttons on the new Settings section publish these envelopes on click and surface success/failure via a transient status line that listens on the status topic.

### Modified Capabilities

- `mqtt-command-envelope` (introduced in `self-upgrading-matrix-controller`): the existing `type=command` dispatch grows three new actions — `force-upgrade`, `restart`, `shutdown`. The envelope shape and route table are unchanged; only the registered handlers set grows. (This change is **additive**, not breaking — existing consumers ignore unknown actions.)

## Impact

- **New files:**
  - `heart-message-manager/templates/settings-pi-upgrade.html` (or section partial extending `settings.html`) — new "Pi Upgrade Control" section with version fields, target-version input, two auto-update checkboxes, and three buttons (force-upgrade, restart, shutdown).
  - `heart-message-manager/static/pi_upgrade_settings.js` — browser-side handler that listens on the status topic for Pi version + auto-update flag (via the existing second `createMqttWsClient` instance from `add-sign-status-reports`), wires target-version input + auto-update checkboxes to Flask API endpoints, and publishes `type=command` envelopes on button click. Shares the existing status-WS client where possible; does not introduce a third WS connection.
  - `heart-matrix-controller/command_handlers.py` — new module holding the three new command handlers (replaces inline registration in `main.py:register_command_handlers` if that helper exists; otherwise is imported by `main.py`).
  - `tests/test_pi_upgrade_settings.py` — tests for the new Flask endpoints and the publish/envelope flow.
- **Modified files:**
  - `heart-matrix-controller/main.py` — register the three new command handlers via `MessageManager.register_handler(action, fn)`.
  - `heart-matrix-controller/loader.py` — on startup, after staging/probe, read the persisted target SHA + auto-update flag from the status flow's most-recent Flask-published settings envelope and decide whether to swap (default behavior: swap if Flask target ≠ active; "Automatically update" off → no swap; "Automatically update" on + target changed → swap if probe succeeds).
  - `lib_shared/message_manager.py` — extend dispatch so the three new `action` values route to their handlers. Existing `check-for-update` handler is unchanged.
  - `lib_shared/models.py` — extend `SignConfig` (or new `PiUpgradeSettings` model) with `target_pi_sha`, `flask_auto_update`, and `pi_auto_update` fields. Schema bumps via additive change; no migration required.
  - `heart-message-manager/main.py` — new endpoints: `GET /api/sign/upgrade-settings` (read), `POST /api/sign/upgrade-settings` (update target + flags), `POST /api/sign/commands/<action>` (publish command envelope). New Flask-side status snapshot publisher (30s cadence on the same status topic, mirror of Pi's `StatusSnapshot.to_mqtt_dict()`).
  - `heart-message-manager/settings.toml.example` + `heart-matrix-controller/settings.toml.example` — no new keys required if we re-use the existing `MQTT_TOPIC` for upgrade-settings envelopes; only the status topic may need a Flask-published counterpart (deferred to `add-sign-status-reports` follow-up if not already there).
  - `heart-message-manager/templates/base.html` — extend `window.APP_CONFIG` with `mqttStatusTopic` (if not already added by `add-sign-status-reports`) so `pi_upgrade_settings.js` can find the status topic without re-parsing the URL.
  - `scripts/startup_matrix_server.sh` + `scripts/lindsay_50.service` — unchanged. Loader still calls itself on boot; the new auto-update-flag logic lives in `loader.py`.
- **No new Python dependencies.** All new paths use existing stdlib (`os`, `subprocess`, `threading`) + existing paho-MQTT client + existing Flask.
- **No new MQTT topics** beyond what `add-sign-status-reports` already adds. The new command envelopes ride the existing `MQTT_TOPIC`.
- **No new Flask routes** beyond the three listed above. No new S3 keys; settings persist in SQLite via `SignConfig`.
- **Boot semantics change.** `loader.py` now consults the persisted target + auto-update flags before staging, where today it stages unconditionally on Flask-hint mismatch. The default (no persisted settings) must remain "stage-and-probe on mismatch" so a fresh install does not silently skip the upgrade.
- **Pi is never bricked.** All three commands (`force-upgrade`, `restart`, `shutdown`) are routed through the loader's existing fallthrough — if anything fails along the way, the loader execs the existing `current/.../main.py` so the Pi can always recover.
