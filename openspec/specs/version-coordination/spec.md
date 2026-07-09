# version-coordination Specification

## Purpose
TBD - created by archiving change self-upgrading-matrix-controller. Update Purpose after archive.
## Requirements
### Requirement: Flask exposes the expected version via boot-config endpoint
Flask MUST expose `GET /api/sign/boot-config` returning `{"expected_sha": "<sha>"}` and nothing else. The SHA MUST be sourced from `lib_shared.boot_config.from_heroku_or_git()`, which returns `HEROKU_SLUG_COMMIT` when set and falls back to `git rev-parse HEAD` for local dev. The endpoint MUST require authentication via the existing `X-API-Key` header.

#### Scenario: Heroku deploy (slug commit set)
- **WHEN** Flask runs on Heroku with `HEROKU_SLUG_COMMIT=abc123`
- **THEN** `GET /api/sign/boot-config` returns `{"expected_sha": "abc123"}` only

#### Scenario: Local dev (no slug commit env var)
- **WHEN** Flask runs locally without `HEROKU_SLUG_COMMIT` set
- **THEN** `GET /api/sign/boot-config` returns `{"expected_sha": <local git HEAD SHA>}`

#### Scenario: Missing or invalid API key
- **WHEN** request to `/api/sign/boot-config` has no `X-API-Key` or an invalid one
- **THEN** endpoint returns 401

### Requirement: Shared `BootConfig` library
`lib_shared/boot_config.py` MUST define `BootConfig` (dataclass with `expected_sha: str`), `fetch_boot_config(api_url, api_key, *, timeout_s=5.0) -> Optional[BootConfig]`, and `from_heroku_or_git()`. Both Flask (server side) and the loader (Pi side) MUST use this module for HTTP retrieval and JSON parsing — no diverging implementations.

`fetch_boot_config` MUST return `None` on any HTTP failure (timeout, 4xx, 5xx, malformed JSON, missing `expected_sha` key) and MUST allow the caller to choose a custom timeout.

#### Scenario: `fetch_boot_config` happy path
- **WHEN** the endpoint returns `{"expected_sha": "abc"}` with HTTP 200
- **THEN** `fetch_boot_config` returns a `BootConfig(expected_sha="abc")`

#### Scenario: `fetch_boot_config` auth failure
- **WHEN** the endpoint returns HTTP 401
- **THEN** `fetch_boot_config` returns `None`

#### Scenario: `fetch_boot_config` network failure
- **WHEN** the request times out, is refused, or DNS fails
- **THEN** `fetch_boot_config` returns `None`

### Requirement: Pi queries the expected SHA on every boot
The Pi's loader MUST call `fetch_boot_config` on every boot before deciding whether to stage a new version. The query MUST use the `X-API-Key` from `settings.toml`.

#### Scenario: Boot against matching version
- **WHEN** Pi boots and `fetch_boot_config` returns the same SHA as local HEAD
- **THEN** loader skips upgrade and execs the existing `current/.../main.py`

#### Scenario: Boot against newer version
- **WHEN** Pi boots and `fetch_boot_config` returns a different SHA than local HEAD
- **THEN** loader stages the expected SHA via worktree, runs the `.status.json` probe, swaps if healthy

#### Scenario: Boot with Flask unreachable
- **WHEN** Pi boots and `fetch_boot_config` returns `None` (timeout, connection refused, 5xx)
- **THEN** loader logs the error and execs the existing `current/.../main.py` without attempting upgrade

### Requirement: Flask publishes a single `check-for-update` hint at startup
Flask MUST publish exactly one `{"type":"command","payload":{"action":"check-for-update"}}` envelope on the configured MQTT topic immediately after the paho client is constructed at startup. The publish MUST use the existing `publish_envelope()` path so QoS 1 + PUBACK semantics are preserved.

The paho client's `on_connect_callback` parameter MUST NOT exist on `PahoMqttClient.__init__` (v2 invariants). Flask MUST NOT publish the envelope on reconnect — only at the initial construction time.

#### Scenario: First Flask start
- **WHEN** Flask starts and the MQTT client is constructed
- **THEN** exactly one `command=check-for-update` envelope is published on `cfg.MQTT_TOPIC`

#### Scenario: Flask restart (deploy, dyno cycle, manual restart)
- **WHEN** Flask restarts (each fresh process is a new "first" start)
- **THEN** exactly one `command=check-for-update` envelope is published on `cfg.MQTT_TOPIC` per process

#### Scenario: MQTT reconnect does not re-publish
- **WHEN** the paho client reconnects to the broker (network blip, broker restart) after the initial connect
- **THEN** no additional `command=check-for-update` envelope is published

#### Scenario: Pi is offline when Flask publishes
- **WHEN** Flask publishes the check-for-update command but the Pi is offline or disconnected from the broker
- **THEN** Flask's publish still completes (QoS 1 PUBACK) and the Pi receives the command on its next broker reconnect, OR (if the Pi is up but hasn't received the hint yet) on its next loader boot

### Requirement: App-side handler routes `check-for-update` via env vars, not reboot
`heart-matrix-controller/check_for_update.py` MUST export `check_for_update(api_url, api_key, repo_dir=None)`. When registered as the `action=check-for-update` handler in `MessageManager`:

- It MUST resolve `active_sha` from `LINDSAY50_ACTIVE_SHA` env var (returning early if missing/empty/whitespace).
- It MUST resolve `repo_dir` from the kwarg, then `LINDSAY50_REPO_DIR` env var, then the conventional `/home/pi/projects/lindsay-50` fallback.
- It MUST call `lib_shared.boot_config.fetch_boot_config` to get the expected SHA.
- If the active SHA equals the expected SHA, it MUST do nothing (no `os.execvpe`).
- If the SHAs differ, it MUST `os.execvpe` into `repo_dir/current/heart-matrix-controller/loader.py` with `LINDSAY50_ACTIVE_SHA=<expected_sha>` set in the env dict.

The handler MUST NOT call `os.system("sudo reboot")` under any condition — reboot is gone in v2.

#### Scenario: SHAs match (most common case)
- **WHEN** the handler runs and `LINDSAY50_ACTIVE_SHA == expected_sha`
- **THEN** the handler logs the match and exits without exec-ing the loader

#### Scenario: Flask unreachable on Pi while running
- **WHEN** the handler runs but `fetch_boot_config` returns `None`
- **THEN** the handler logs the error and exits without exec-ing the loader

#### Scenario: SHAs differ (upgrade available)
- **WHEN** the handler runs and `LINDSAY50_ACTIVE_SHA != expected_sha`
- **THEN** the handler calls `os.execvpe(python, [python, repo_dir/current/.../loader.py], env={..., LINDSAY50_ACTIVE_SHA: expected_sha})` — the loader inherits the env vars and continues the upgrade flow

#### Scenario: First boot after `setup-pi.sh`
- **WHEN** the handler runs and `LINDSAY50_ACTIVE_SHA` is not in the environment
- **THEN** the handler logs and exits without exec-ing the loader (loader already handled the boot end-to-end)

