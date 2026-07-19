## 1. Backend: extend SignSettings with target_version

- [ ] 1.1 Add `target_version: str` to the `SignSettings` dataclass in `lib_shared/models.py` (additive). The default resolves to Flask's running short SHA at construction time (via `from_heroku_or_git()` + `short_sha()`). The wire form is always concrete — Flask never persists an empty `target_version`.
- [ ] 1.2 Update `SignSettings.from_dict` so a missing `target_version` field falls back to the Flask running short SHA (forward-compatible: pre-change Flask publishes `type=config` payloads without `target_version`, and the Pi reads via the dataclass default).
- [ ] 1.3 Update `SignSettings.to_dict` to always include `target_version`.
- [ ] 1.4 Verify the existing `POST /api/config` endpoint accepts the new `sign.target_version` field — the round-trip must include it. Empty input from the form MUST be resolved to Flask's running short SHA by Flask before persisting (server-side resolution), not stored as `""` or `null`.
- [ ] 1.5 Confirm SQLite-from-S3 rebuild path (`heart-message-manager/sqlite.py`) round-trips the new field. The S3 rebuild re-creates the `SignSettings` row with `target_version` initialized to Flask's running short SHA.

## 2. Backend: New GET /api/sign/settings endpoint

- [ ] 2.1 In `lib_shared/boot_config.py`, add `fetch_sign_settings(api_url, api_key, timeout=5.0)` that hits `GET /api/sign/settings` and returns the parsed dict (or `None` on any failure: timeout, HTTP error, malformed JSON, missing `target_version` field).
- [ ] 2.2 In `heart-message-manager/main.py`, add a Flask route `GET /api/sign/settings` that requires the existing `api_login_required` auth and returns `{"target_version": "<short-sha>", "timezone": "<iana>"}`. Both fields are resolved from the persisted `SignConfig` (`sign.target_version` and top-level `SignConfig.timezone`). The existing `GET /api/sign/boot-config` route is **unchanged** (kept for legacy Pis).
- [ ] 2.3 Add a test (`tests/test_sign_settings_endpoint.py`) verifying: returns 200 + concrete `target_version` when operator-pinned; returns 200 + Flask SHA when `target_version` is empty; returns 401 on missing/invalid `X-API-Key`; never returns `target_version: null` or `target_version: ""`.
- [ ] 2.4 Add a test (`tests/test_fetch_sign_settings.py`) verifying: `fetch_sign_settings` returns the parsed dict on 200 response; returns `None` on timeout, HTTP 5xx, malformed JSON, or missing `target_version`.

## 3. Backend: Pi command handlers

- [ ] 3.1 Create `heart-matrix-controller/command_handlers.py` with three functions: `force_upgrade(payload)`, `restart(payload)`, `shutdown(payload)`. Each takes a `payload` dict (per the existing `MessageManager.dispatch` contract) and returns `None`.
- [ ] 3.2 `force_upgrade` resolves `LINDSAY50_REPO_DIR` from env (default `/home/pi/projects/lindsay-50`), builds `loader_argv = [sys.executable, "<repo_dir>/heart-matrix-controller/loader.py", *sys.argv[1:]]`, sets env `LINDSAY50_ACTIVE_SHA` (short form) if known, and calls `os.execvpe(sys.executable, loader_argv, env)`. If the exec raises (loader script missing), logs and continues — `main.py` is unaffected.
- [ ] 3.3 `restart` calls `subprocess.run(["sudo", "reboot"], check=False)` and logs non-zero exits.
- [ ] 3.4 `shutdown` calls `subprocess.run(["sudo", "shutdown", "-h", "now"], check=False)` and logs non-zero exits.
- [ ] 3.5 All three handlers log their invocation at INFO level with the action name and a small payload summary.
- [ ] 3.6 In `heart-matrix-controller/main.py:register_command_handlers`, register the three new handlers via `MessageManager.register_handler`. The existing `check-for-update` registration is **kept** during the transitional period (removed in a follow-up change once all Pis are on the new code).

## 4. Backend: New POST /api/sign/commands/<action> endpoints

- [ ] 4.1 In `heart-message-manager/main.py`, add three Flask routes: `POST /api/sign/commands/force-upgrade`, `POST /api/sign/commands/restart`, `POST /api/sign/commands/shutdown`. Each requires `api_login_required` auth, publishes the corresponding envelope (`{"type":"command","payload":{"action":"<name>"}}`) via the existing `PahoMqttClient.publish_envelope` path, and returns 202 on success / 503 on publish failure.
- [ ] 4.2 Add a test (`tests/test_flask_command_endpoints.py`) verifying: each endpoint returns 202 on success; each publishes the expected envelope shape (use a mock MQTT client); each returns 503 when publish fails; each requires `X-API-Key`.

## 5. Backend: Loader uses GET /api/sign/settings + short SHA comparison

- [ ] 5.1 In `heart-matrix-controller/loader.py`, replace the existing `GET /api/sign/boot-config` HTTP call with a `GET /api/sign/settings` call (via `fetch_sign_settings`). On success, read `target_version` from the response.
- [ ] 5.2 On any failure (timeout, HTTP error, malformed response, missing field), the loader MUST fall through to running `current/.../main.py` without upgrade — same safe default as the existing `boot-config` failure path.
- [ ] 5.3 Truncate `local = short_sha(git rev-parse HEAD)` (7-char truncation) before the equality comparison. The comparison becomes `short_sha(local) == target_version` (was `local == expected_sha`). This is the only loader-internal change needed to drop the long form.
- [ ] 5.4 The loader's `BOOT_HOLD_S = 17.0` (set by the merged `add-sign-status-reports` change) and `.status.json` probe remain the source of truth for "is the new version safe to swap to?".
- [ ] 5.5 `AUTO_UPDATE` is read from `settings.toml` via the existing `config_reader.py` path (env override: `AUTO_UPDATE=false`). No change to the existing read path at `loader.py:833`.
- [ ] 5.6 Add a public `force_upgrade()` entrypoint to `loader.py` that the `command_handlers.force_upgrade` handler can `os.execvpe` into. Same SHA-check + stage + probe + swap logic as the boot path; just bypasses the `AUTO_UPDATE` check.
- [ ] 5.7 Add a test (`tests/test_loader_sign_settings_fetch.py`) verifying: loader calls `/api/sign/settings`; on success uses `target_version`; on failure falls through to `current/.../main.py`; comparison truncates local to 7 chars; force-upgrade bypasses `AUTO_UPDATE`.

## 6. Frontend: Settings UI section

- [ ] 6.1 Add a new "Pi Upgrade Control" section to `heart-message-manager/templates/settings.html` (Bootstrap 5 variant) with three read-only version displays (Flask version, Pi version, current Target Pi version), one editable Target Pi version input (with a "Clear" button alongside), and three command buttons. **No `AUTO_UPDATE` checkbox in v1** — that knob is settings.toml-only (pre-existing).
- [ ] 6.2 Mirror the same section in `templates/settings-playful.html` (Tailwind) with the same field semantics and Tailwind styles.
- [ ] 6.3 Use `data-sign-status-field="<name>"` attributes for the Flask version + Pi version fields so `sign_status.js` and `pi_upgrade_settings.js` can co-populate from the same status-WS payloads.
- [ ] 6.4 Use `data-upgrade-settings-field="target_version"` for the Target input; use `data-action="clear-target"` for the Clear button.
- [ ] 6.5 The Save button on the Target Pi version input submits through the existing `POST /api/config` endpoint (no new endpoint). Empty input → resolved to Flask SHA server-side before persisting.

## 7. Frontend: JS module

- [ ] 7.1 Create `heart-message-manager/static/pi_upgrade_settings.js` as a new ES module that listens on the existing status-WS client (`createMqttWsClient` from `mqtt_ws_client.js`) for `source: "flask"` and `source: "pi"` snapshots.
- [ ] 7.2 Populate `data-sign-status-field` slots (Flask version, Pi version) from the merged snapshot map. Fallback: when no snapshot received, display "No status received yet" (Pi) and "loading…" (Flask, since Flask's status always starts within 5s of boot).
- [ ] 7.3 Populate the `target_version` input from the persisted `SignConfig` (load-time fetch from `GET /api/sign/settings`); wire Save → `POST /api/config` with `sign.target_version` in the JSON body. No debouncing on Save (matches the existing settings form pattern).
- [ ] 7.4 Wire the Clear button to empty the `target_version` input and trigger the same Save flow (empty → Flask SHA via server-side resolution).
- [ ] 7.5 Wire the three command buttons to `POST /api/sign/commands/<action>` after a `confirm()` dialog; force-upgrade uses simple text; restart and shutdown use explicit text including the word "shutdown"/"restart".
- [ ] 7.6 Show a transient status line ("Force upgrade sent — Pi is updating…", "Restart command sent — check Pi", etc.) for each command, sourced from the next status-snapshot state change.

## 8. Tests

- [ ] 8.1 `tests/test_sign_settings_default_target_version.py` — `SignSettings.target_version` defaults to Flask's running short SHA at construction time; round-trip through `to_dict()` / `from_dict()`; missing field falls back to default; SQLite round-trip; S3-rebuild re-creates with default.
- [ ] 8.2 `tests/test_sign_settings_endpoint.py` — covered by task 2.3.
- [ ] 8.3 `tests/test_fetch_sign_settings.py` — covered by task 2.4.
- [ ] 8.4 `tests/test_flask_command_endpoints.py` — covered by task 4.2.
- [ ] 8.5 `tests/test_pi_command_handlers.py` — `force_upgrade` builds the right loader argv + env; `restart`/`shutdown` call subprocess with the right args; exceptions are isolated and logged.
- [ ] 8.6 `tests/test_loader_sign_settings_fetch.py` — covered by task 5.7.
- [ ] 8.7 Browser test (Pyodide in PyScript): render the Settings page with a mocked status snapshot, assert Flask version + Pi version fields populate; click Save on a non-empty Target input, assert the field is persisted (mock the POST); click Clear, assert the input empties; click each command button and assert the right `POST /api/sign/commands/<action>` envelope is published.

## 9. Documentation and rollout

- [ ] 9.1 Update `CLAUDE.md` (project instructions) with the new admin-UI section, the `sign.target_version` field on `SignSettings`, the three command endpoints, the kept `boot-config` and `check-for-update` legacy paths, and the operator-facing intent of each knob.
- [ ] 9.2 Update `CHANGELOG.md` with a short entry summarizing the change.
- [ ] 9.3 Run `PYTHONPATH=. pytest tests/ -v` and capture output for the change record.
- [ ] 9.4 Manual smoke test (operator runs locally): spin up Flask, visit `/settings`, observe version fields populate from a published mock status snapshot, type a SHA into Target Pi version, click Save and confirm SQLite + MQTT envelope; click Force-upgrade and confirm the published envelope shape (the actual Pi handler is unreachable in laptop dev, but the publish path must work).
- [ ] 9.5 Manual Pi smoke (operator runs on the Pi): pin a target SHA via the Settings page, confirm the `type=config` envelope reaches the Pi and the loader holds/swaps accordingly; confirm the loader calls `GET /api/sign/settings` on boot and uses the resolved `target_version`; click Force upgrade and confirm the loader exec runs.
- [ ] 9.6 Manual destructive smoke (operator runs on the Pi with confirmation): click Restart and confirm the Pi reboots and the loader recovers to a working display; click Shutdown and confirm the Pi halts; power back on and confirm the loader rebuilds display.

## 10. Follow-up change (parked)

- [ ] 10.1 Once all deployed Pis are confirmed to be on the new code, create a follow-up change to: delete `GET /api/sign/boot-config` route; remove `check-for-update` registration from `main.py`; delete `heart-matrix-controller/check_for_update.py`; stop Flask from publishing the one-shot `check-for-update` envelope at startup. Out of scope for this change.