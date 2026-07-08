## 1. Backend: model and persistence

- [ ] 1.1 Add `PiUpgradeSettings` dataclass to `lib_shared/models.py` with fields `target_pi_sha: str`, `flask_auto_update: bool`, `pi_auto_update: bool`, plus a `settings_version: int = 1` discriminator
- [ ] 1.2 Add a SQLite table `pi_upgrade_settings` (single-row) to `heart-message-manager/sqlite.py` schema with the same idempotent CREATE-IF-NOT-EXISTS pattern used by `sign_config`
- [ ] 1.3 On Flask startup, ensure the row exists; default values are `target_pi_sha = HEROKU_SLUG_COMMIT (or git fallback)`, `flask_auto_update = True`, `pi_auto_update = True`
- [ ] 1.4 Extend the S3-rebuild-from-S3 path so a fresh SQLite snapshot re-creates the `pi_upgrade_settings` row with the same defaults

## 2. Backend: Flask endpoints

- [ ] 2.1 Add `GET /api/sign/upgrade-settings` returning the persisted row as JSON, with `X-API-Key` auth matching existing admin contract
- [ ] 2.2 Add `POST /api/sign/upgrade-settings` accepting `target_pi_sha`, `flask_auto_update`, `pi_auto_update`; persists to SQLite and publishes one `set-upgrade-settings` envelope
- [ ] 2.3 Add `POST /api/sign/commands/<action>` with `<action>` in {`force-upgrade`, `restart`, `shutdown`}; publishes the corresponding `type=command` envelope and returns 202 (or 503 on publish failure)
- [ ] 2.4 Add a `threading.Timer`-driven Flask-side status publisher that emits a snapshot on `MQTT_STATUS_TOPIC` every 30s with `source: "flask"`, `active_sha`, `started_at`, `uptime_seconds`
- [ ] 2.5 Confirm `MQTT_STATUS_TOPIC` is configured for Flask too (extend `settings.toml.example` if not already added by `add-sign-status-reports`)

## 3. Backend: Pi command handlers

- [ ] 3.1 Create `heart-matrix-controller/command_handlers.py` with three functions: `force_upgrade(payload)`, `restart(payload)`, `shutdown(payload)`
- [ ] 3.2 `force_upgrade` builds the loader argv from `LINDSAY50_REPO_DIR`, sets `LINDSAY50_ACTIVE_SHA` to the currently-known active SHA if known, and calls `os.execvpe`
- [ ] 3.3 `restart` calls `subprocess.run(["sudo", "reboot"], check=False)` and logs non-zero exits
- [ ] 3.4 `shutdown` calls `subprocess.run(["sudo", "shutdown", "-h", "now"], check=False)` and logs non-zero exits
- [ ] 3.5 All three handlers log their invocation at INFO level with the action name and a small payload summary
- [ ] 3.6 In `heart-matrix-controller/main.py`, register all three new handlers via `MessageManager.register_handler` after the existing `check-for-update` registration

## 4. Backend: Pi receives settings envelope

- [ ] 4.1 Add `set_upgrade_settings(payload)` to `command_handlers.py` (or to `check_for_update.py` if colocated with the existing handler): stores latest `target_pi_sha`, `flask_auto_update`, `pi_auto_update` in a process-local `PiUpgradeState` object
- [ ] 4.2 Register `set-upgrade-settings` action in `main.py:register_command_handlers`
- [ ] 4.3 If the new target differs from `LINDSAY50_ACTIVE_SHA` AND both auto-update flags are True, trigger `check_for_update` semantics (re-use the existing handler)
- [ ] 4.4 Update `heart-matrix-controller/status.py` so its `StatusSnapshot.to_mqtt_dict()` includes `pi_auto_update` and the latest known `target_pi_sha` (read from `PiUpgradeState`)
- [ ] 4.5 The Pi never blocks on settings-envelope processing — fall through to `main.py` if the handler raises

## 5. Backend: Loader consults persisted flags

- [ ] 5.1 In `heart-matrix-controller/loader.py`, before staging, read the latest `target_pi_sha` + `flask_auto_update` + `pi_auto_update` from `PiUpgradeState` (set by 4.1) — fall back to "always stage on SHA mismatch" if `PiUpgradeState` is empty (preserves the existing behavior on a fresh install)
- [ ] 5.2 Implement the upgrade decision from design D3: target ≠ active AND flask-auto-update on AND pi-auto-update on → stage + probe + swap; otherwise leave alone
- [ ] 5.3 Force-upgrade path (`os.execvpe` into loader) skips the auto-update-flag check but still uses the persisted `target_pi_sha`
- [ ] 5.4 Add a 15-second `threading.Timer` after loader-stage that, if no swap happens, logs the hold-back rationale at INFO level so operators can diagnose

## 6. Frontend: Settings UI section

- [ ] 6.1 Add a new "Pi Upgrade Control" section to `heart-message-manager/templates/settings.html` (Bootstrap 5 variant) with three read-only version displays, target-version input, two auto-update checkboxes, and three command buttons
- [ ] 6.2 Mirror the same section in the playful template (`templates/settings-playful.html` or equivalent) using Tailwind styles
- [ ] 6.3 Use `data-sign-status-field="<name>"` attributes for the Flask version + Pi version + Pi-auto-update fields so `sign_status.js` and `pi_upgrade_settings.js` can co-populate
- [ ] 6.4 Use `data-upgrade-settings-field="<name>"` attributes for target_pi_sha / flask_auto_update inputs

## 7. Frontend: JS module

- [ ] 7.1 Create `heart-message-manager/static/pi_upgrade_settings.js` as a new ES module that listens on the existing status-WS client for `source: "flask"` and `source: "pi"` snapshots
- [ ] 7.2 Populate `data-sign-status-field` and `data-upgrade-settings-field` slots from the merged snapshot map
- [ ] 7.3 Wire the target-version input + Flask-auto-update checkbox to a `POST /api/sign/upgrade-settings` save (with debounced save on change)
- [ ] 7.4 Wire the three command buttons to `POST /api/sign/commands/<action>` after a `confirm()` dialog; force-upgrade dialog is non-destructive text; restart and shutdown use explicit text including the word "shutdown"/"restart"
- [ ] 7.5 Show a transient status line ("Force upgrade sent — Pi is updating…", "Restart command sent — check Pi", etc.) for each command, sourced from the next status-snapshot state changes
- [ ] 7.6 Update `heart-message-manager/templates/base.html` `window.APP_CONFIG` to include `mqttStatusTopic` if not already present (per `add-sign-status-reports`)

## 8. Tests

- [ ] 8.1 `tests/test_pi_upgrade_settings_persistence.py` — round-trip the `PiUpgradeSettings` row through SQLite, including S3-rebuild re-creation
- [ ] 8.2 `tests/test_flask_upgrade_endpoints.py` — `GET /api/sign/upgrade-settings` and `POST /api/sign/upgrade-settings` happy paths + auth failures
- [ ] 8.3 `tests/test_flask_command_endpoints.py` — each of `force-upgrade`, `restart`, `shutdown` returns 202 and publishes the expected envelope (use a mock MQTT client)
- [ ] 8.4 `tests/test_pi_command_handlers.py` — `force_upgrade` execs into the loader argv; `restart`/`shutdown` call subprocess with the right args; exceptions are isolated and logged
- [ ] 8.5 `tests/test_pi_upgrade_state.py` — `set_upgrade_settings` updates the in-memory state; subsequent `check_for_update` triggers when conditions met
- [ ] 8.6 `tests/test_loader_upgrade_decision.py` — `loader.py` honors the auto-update flags when present, falls through to existing behavior when state is empty
- [ ] 8.7 Browser test (Pyodide in PyScript): render the Settings page with mocked status snapshot, assert version fields populate and Force-upgrade button click publishes the expected envelope shape (extend `tests/test_settings_playful.py` or add a new module)
- [ ] 8.8 `tests/test_mqtt_status_publisher.py` — Flask-side status publishes on 30s cadence with the expected payload shape

## 9. Documentation and rollout

- [ ] 9.1 Update `CLAUDE.md` (project instructions) with the new admin-UI section, command endpoints, and the operator-facing intent of each flag
- [ ] 9.2 Update `heart-matrix-controller/settings.toml.example` and `heart-message-manager/settings.toml.example` with any new keys (`MQTT_STATUS_TOPIC`, etc.)
- [ ] 9.3 Run `PYTHONPATH=. pytest tests/ -v` and capture output for the change record
- [ ] 9.4 Manual smoke test: spin up Flask locally, visit `/settings`, observe version fields populate from a published mock status snapshot, click Save and confirm SQLite + MQTT envelope; click Force-upgrade and confirm the published envelope shape (the actual Pi handler is unreachable in laptop dev, but the publish path must work)
- [ ] 9.5 Manual Pi smoke (operator runs on the Pi): toggle Flask auto-update off, confirm `set-upgrade-settings` envelope reaches the Pi and the Pi holds the current version; toggle back on, confirm upgrade resumes; click Force upgrade, confirm the loader exec runs
- [ ] 9.6 Manual destructive smoke (operator runs on the Pi with confirmation): click Restart and confirm the Pi reboots and the loader recovers to a working display; click Shutdown and confirm the Pi halts; power back on and confirm the loader rebuilds display
