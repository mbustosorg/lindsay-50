# Changelog

All notable changes to lindsay-50 are documented in this file.

## [Unreleased] — 2026-07-19

### Added — Pi upgrade controls (issue #51, `openspec_change_name: add-pi-upgrade-controls`)

The Settings page now exposes operator-driven Pi upgrade controls,
paired with three new Flask endpoints and a Pi-side command registry.
The loader migrates from `/api/sign/boot-config` to `/api/sign/settings`
for the target version, and short-vs-short comparison replaces the
legacy full-SHA matching. AUTO_UPDATE stays in `settings.toml` — no UI
checkbox in v1.

**New endpoints (Flask):**

- `GET /api/sign/settings` — returns `{target_version, timezone}` where
  `target_version` is always a concrete 7-char short SHA. Reads the
  operator-pinned value from `cfg.sign.target_version`; if empty,
  falls back to the Flask's own running short SHA. Backend truncates
  to 7 chars at serialization time so the wire form is deterministic.
- `POST /api/sign/commands/<action>` — publishes a `type=command`
  envelope on the existing `MQTT_TOPIC`. Valid actions: `force-upgrade`,
  `restart`, `shutdown`. Returns 202 on publish, 503 on broker failure,
  404 on unknown action, 401 on missing/bad X-API-Key.

**Pi-side dispatcher (hand-side):**

- `heart-matrix-controller/command_handlers.py` — three zero-arg
  handlers (`force_upgrade`, `restart`, `shutdown`) registered via the
  new `MessageManager.register_handler` action registry. `force-upgrade`
  uses `os.execvpe` with `LINDSAY50_FORCE_UPGRADE=1` to enter the
  loader's force-upgrade entrypoint (which bypasses `AUTO_UPDATE`).
- The legacy `check-for-update` handler still works via the
  `on_check_for_update` constructor kwarg; the registry takes
  precedence when both are wired.

**Loader changes:**

- New `fetch_target_version` calls `/api/sign/settings`; the legacy
  `fetch_expected_sha` (which calls `/api/sign/boot-config`) is kept
  as a transitional safety net.
- Short-vs-short comparison: `local` is the full SHA from
  `git rev-parse HEAD`; `target_version` is already the 7-char form.
- New `force_upgrade_main()` entrypoint bypasses the `AUTO_UPDATE`
  gate but uses the same SHA-check + stage + probe + swap logic as
  the regular `main()`.

**UI:**

- New "Pi Upgrade Control" section on `templates/settings.html` with
  a Target Pi version input + Apply button + three command buttons
  (force upgrade / restart / shutdown) that POST to the new Flask
  endpoints via `static/pi_upgrade_settings.js`. Each command is
  gated behind a `confirm()` modal.
  - v2 update: 3-column layout (Flask / Target / Running) with
    thin-outline styling on all three cells. The legacy Clear
    button was removed — operators delete text directly. When the
    Target field is empty the Flask version is shown as muted
    grey placeholder text. Clicking the empty field clears it
    so the operator can type (handled by `pi_apply_settings.js`).
    The Apply button is disabled by default and enabled when the
    input differs from its saved value.

**New static JS module:**

- `static/pi_apply_settings.js` — Apply button + click-to-edit for
  the Target Pi version input. The script reads
  `data-saved-value` (the persisted target_version) and
  `data-flask-version-placeholder` (the rendered HTML `placeholder`)
  to drive dirty-state and focus-clear. Click on Apply submits the
  surrounding `<form method="POST">` via `form.requestSubmit(applyBtn)`,
  reusing Flask's existing `/settings` handler.

**Behaviour:**

- `/settings` POST now publishes a `command=check-for-update` envelope
  on the same topic when `cfg.sign.target_version` changed between
  pre-POST snapshot and post-POST value (including explicit clearing).
  This mirrors the startup-time hint and routes the Pi through the
  same `MessageManager.register_handler("check-for-update", ...)`
  handler — AUTO_UPDATE-gated, falls back to Flask's running short SHA
  on empty. Force-upgrade remains the AUTO_UPDATE-bypass path.
- `sign.target_version` is `cfg.sign.target_version` on the Flask side.
  An empty form value clobbers to empty (no longer preserves the prior
  pinned value) so the operator's explicit clear is honored end-to-end
  on the wire. The /api/sign/settings handler still does the
  Flask-version fallback at read time, so the wire form is always
  concrete.
- `force-upgrade` preserves signal handling via `os.execvpe` so the
  systemd unit sees the loader as the PID's direct child.
- AUTO_UPDATE remains operator-only — no checkbox is rendered.

**Operational notes:**

- Old Pis (before issue #51) keep working because `/api/sign/boot-config`
  is unchanged. The new code uses `/api/sign/settings` exclusively.
- Pi-side updates are not rolled back automatically; the operator
  uses `heroku rollback v<N>` if a regression appears after `force-upgrade`.

## [Unreleased] — 2026-07-09

### Added — Sign status reports over MQTT (issue #15, `openspec_change_name: add-sign-status-reports`)

The Pi now publishes a `StatusSnapshot` to a dedicated `MQTT_STATUS_TOPIC`
on a 5-second cadence, unified with the existing `.status.json` file
write — both happen in the same `StatusWriter.tick()` call. One
throttle constant, one code path, one cadence. The wire payload is the
8-key `StatusSnapshot` shape (`schema_version`, `active_sha`,
`short_sha`, `started_at`, `updated_at`, `uptime_seconds`,
`mqtt_connected`, `last_error`); the `pid`, `messages_rendered`, and
`last_tick_age_ms` fields were dropped — they had no consumer.

**MQTT path.** A long-lived paho publisher
(`heart-matrix-controller/status_publisher.py:StatusPublisher`)
handles the network on a background thread (`connect_async` +
`loop_start`); `client.publish()` is thread-safe and non-blocking. QoS
0 fire-and-forget means a flaky broker can't stall the render loop.
A 5s reconnect timer fires when `publish()` returns a non-`SUCCESS`,
non-`NO_CONN` rc; `MQTT_ERR_NO_CONN` is treated as transient (paho's
loop thread handles CONNACK retries on its own, the timer would just
pile up redundant reconnects).

**Flask side.** `lib_shared/sign_status.py:LatestSignStatus` holds the
most recent snapshot in a `threading.RLock`-guarded in-memory store
(defensive-copy semantics, ISO-8601 `received_at_wallclock()`
timestamp). A new Flask route `GET /api/sign-status` returns the
latest snapshot (always 200; the snapshot field is `null` when none
has been received yet) — used by the browser for load-time hydration.
Flask subscribes to `MQTT_STATUS_TOPIC` via the dual-topic
`PahoMqttClient` extension (`status_topic` + `status_dispatch_callback`
kwargs); the envelope subscription on `MQTT_TOPIC` is independent.

**Browser side.** A single load-time `fetch('/api/sign-status')`
hydrates the UI; a second `createMqttWsClient` instance scoped to
`MQTT_STATUS_TOPIC` carries live updates after load. A 5s `setInterval`
re-evaluates the pill state even when no new snapshot arrives. The
Dashboard's hardcoded green "Live" pill became a dynamic 4-state
element driven by snapshot age AND snapshot contents:

| State | Trigger |
|---|---|
| **Live (healthy)** — green pulse | Snapshot < 15s old AND `mqtt_connected === true` AND `last_error` empty |
| **Live (degraded)** — amber | Snapshot < 15s old AND (`mqtt_connected === false` OR `last_error` non-empty) |
| **Unknown** — amber | Snapshot 15–30s old |
| **Offline** — grey | Snapshot > 30s old OR never received |

The Settings page gained a **Sign Health** section above "Sign Name"
showing all 8 snapshot fields plus the browser receive timestamp;
when `health=degraded`, a warning banner names the failing check
("MQTT disconnected", "Last error: <message>"). The script is a
no-op on pages with neither `#sign-live-pill` nor
`[data-sign-status-field]`.

**Topic derivation.** Default rule: `MQTT_STATUS_TOPIC` is empty in
`settings.toml` and resolves to `f"{MQTT_TOPIC}-status"`. Operators
override by setting `MQTT_STATUS_TOPIC` in `settings.toml` or via
the env var (env wins). On Adafruit IO the operator MUST create the
derived feed in the AIO dashboard before the first publish — the
broker silently drops publishes to non-existent feeds. The resolved
topic is exposed via `window.APP_CONFIG.mqttStatusTopic` in
`base.html` so `sign_status.js` can find it without re-parsing the
URL.

**New files.**

- `heart-matrix-controller/status_publisher.py` — `StatusPublisher`
  (long-lived paho, single client + `connect_async` + `loop_start`,
  thread-safe `publish()` at QoS 0, defensive reconnect on
  non-`SUCCESS` rc, idempotent `close()`).
- `lib_shared/sign_status.py` — `LatestSignStatus` (Flask-side
  in-memory holder, RLock-guarded, defensive-copy semantics, ISO-8601
  `received_at_wallclock()` timestamp).
- `heart-message-manager/static/sign_status.js` — Browser module
  (load-time fetch + WS subscription + 5s re-render timer, renders
  Dashboard pill and Settings-page Sign Health section).

**Modified files.**

- `heart-message-manager/main.py` — added dual-topic subscribe on
  `PahoMqttClient`, `GET /api/sign-status` route, `mqttStatusTopic`
  injection into `APP_CONFIG`.
- `heart-matrix-controller/main.py` — `StatusPublisher` instantiated
  and handed to `StatusWriter` via `status_publisher=`.
- `heart-matrix-controller/status.py` — `StatusSnapshot` field set
  frozen to 8 keys (the `pid`/`messages_rendered`/`last_tick_age_ms`
  fields were dropped across the whole system).
- `heart-matrix-controller/loader.py` — `BOOT_HOLD_S` updated from
  8s to 17s (3 × 5s cadence + 2s slack) so the pre-swap probe's
  "3 missed writes" confidence matches the dashboard pill's 15s
  `live` window. `.status.json` mtime remains the sole loader health
  signal — no MQTT-based loader logic added.
- `lib_shared/paho_mqtt_client.py` — added optional `status_topic` +
  `status_dispatch_callback` kwargs to the constructor; subscribe loop
  in the daemon thread handles both topics independently.
- `heart-message-manager/templates/{base,dashboard,settings}.html` —
  Dashboard pill became dynamic, Settings gained Sign Health section,
  `mqttStatusTopic` injected into `APP_CONFIG`.

## [Unreleased] — 2026-07-02

### Added — MMS image and video attachments (issue #38, `openspec_change_name: add-image-and-video-support`)

The Flask webhook now ingests MMS attachments: Twilio's `MediaUrl0..`
links are downloaded to S3 (`media/images/<YYYY-MM>/` and
`media/videos/<YYYY-MM>/`, mirroring the messages archive layout),
the `Message` wire shape carries a `media: list[{type, url}]` field,
and the Pi's `EffectsCoordinator` constructs a `MediaCycler` per
message at the out→in fade transition so each attachment renders as
the background effect while the text scrolls.

PngDisplay is now `ImageDisplay` (PNG / JPEG / GIF / WebP); `PngDisplay`
is gone. The effect registry moved out of `models.py:_DEFAULT_EFFECTS_LIST`
into a JSON-driven loader (`lib_shared/effects_loader.py`,
`config/effects.json`); operators override via the `EFFECTS_SETTINGS_OVERRIDE`
env var and the `/settings` admin page renders the merged list
verbatim.

A new Flask route `GET /api/media/<key>` 302s each request to a freshly-
signed S3 URL behind `api_login_required` — both Pi and browser follow
the same redirect, so S3 credentials stay server-side. The preview's
`MediaCycler` analogue is a DOM-driven `BrowserMediaOverlay` (no
PIL/cv2 in Pyodide): `<img>` / `<video>` elements positioned over the
LED canvas, swapped from `current_media_url` each frame.

The admin `/messages` table now has a Media column with thumbnails
(image), play-badge (video), and a click-to-zoom lightbox modal. The
SQLite schema gained a `media TEXT` column with an in-place
`ALTER TABLE ADD COLUMN` migration for pre-issue-38 databases.

Out of scope for this change: pre-caching attachments on receive
(section 11.5). The Pi's cycler fetches each attachment lazily on
cycle advance; the browser's `<img>` / `<video>` fetches on demand.

### Added — Self-upgrading Pi matrix controller (issue #49)

The Pi matrix controller now upgrades itself whenever Flask restarts with a
new commit. The Pi is no longer a thing you have to `ssh` into to update.

**Flow.** When Flask restarts (e.g. `git push heroku main`), it now
publishes a *one-shot* MQTT `command=check-for-update` envelope at
startup. The Pi's running `main.py` receives the envelope, looks up its
own SHA from the `LINDSAY50_ACTIVE_SHA` env var, asks Flask for the
expected SHA via `GET /api/sign/boot-config`, and if they differ it
`os.execvpe`s into the Pi's `loader.py`. The loader queries Flask
itself, stages the new commit into `git worktree add v-<sha>`, waits
for the staged worktree's `.status.json` to report `mqtt_connected=true`
with no `last_error`, then atomically swaps the `current` symlink and
`os.execvpe`s the new version. Because `LINDSAY50_ACTIVE_SHA` is set in
the env passed to the new `main.py`, the cycle is closed: the running
app always knows its own SHA, and the loader can compare against it
without reading `.status.json`.

**Why not just reboot?** The first draft of this feature had the MQTT
envelope trigger `sudo reboot` and the loader probe the staged version
via a `--healthcheck` subprocess flag. Two problems showed up:

1. paho reconnects several times on bad networks. Re-publishing the
   envelope on every reconnect turned broker flaps into reboot storms.
2. Rebooting the Pi to swap versions is wasteful — most Flask restarts
   (config:set, dyno cycle) don't change `HEROKU_SLUG_COMMIT`. v2
   publishes the hint exactly once at startup, and the Pi compares
   SHAs before doing anything. Same-SHA → no-op.

**Why `.status.json` instead of `--healthcheck`?** The `--healthcheck`
subprocess flag + post-swap grace period worked, but it ran `main.py`
in a subprocess and waited for an exit code, leaving a 30s window where
the new version was on screen and a 30s period of dark pixels on every
failure. v2 reads `.status.json` against the **staged** worktree before
the swap, so a bad release is detected and rejected with zero
downtime on the running version.

**Rollback.** `heroku rollback v123` is the rollback primitive. The
operator no longer SSHes in for routine rollbacks; the next Flask
restart publishes `check-for-update`, the Pi pulls v123.

**Operator workflow changes.**

- `heroku rollback v123` is now the rollback primitive — it sets
  `HEROKU_SLUG_COMMIT` to v123's hash, Flask restarts, publishes
  `command=check-for-update`, and the Pi pulls v123 on its own.
- One-time Pi bootstrap (~1 minute of downtime): `sudo systemctl stop
  lindsay_50` → `git pull` → `sudo scripts/setup-pi.sh`. The script is
  idempotent and converts the existing clone to a bare-repo + per-SHA
  worktrees + `current` symlink layout. After bootstrap, systemd sets
  `LINDSAY50_REPO_DIR` for the loader and `main.py`.

**New files.**

- `heart-matrix-controller/loader.py` — the upgrade orchestrator
  (query → stage → status.json probe → swap → exec).
- `heart-matrix-controller/check_for_update.py` — the
  `action=check-for-update` MQTT handler. Reads
  `LINDSAY50_ACTIVE_SHA` from env, compares to Flask's expected SHA,
  `os.execvpe`s into the loader on mismatch.
- `heart-matrix-controller/status.py` — atomic, throttled (3s)
  writer for `$REPO_DIR/.status.json`. The loader's pre-swap probe
  reads this file.
- `lib_shared/boot_config.py` — shared `BootConfig` dataclass +
  `fetch_boot_config` + `from_heroku_or_git()`. Used by both Flask
  (serving the endpoint) and the loader (querying it).

**Modified files.**

- `lib_shared/message_manager.py` — `dispatch()` now routes
  `type=command` envelopes through a `command_handlers` mapping
  (constructable + late-registerable); the v1 hardcoded `sudo reboot`
  path is replaced by `check_for_update.check_for_update`.
- `lib_shared/paho_mqtt_client.py` — the v1 `on_connect_callback`
  kwarg was removed; v2 publishes the hint once at Flask startup,
  not on every MQTT reconnect.
- `heart-message-manager/main.py` — added `GET /api/sign/boot-config`
  (auth-gated, response shape just `{"expected_sha": "..."}`); the
  one-shot `check-for-update` hint is published right after the paho
  client is constructed.
- `heart-matrix-controller/main.py` — `--healthcheck` argparse
  flag is gone (no longer needed; the loader probes via
  `.status.json`). The render loop now constructs a `StatusWriter`
  keyed on its tick.
- `scripts/lindsay_50.service` + `scripts/startup_matrix_server.sh`
  — ExecStart points at the loader. `StartLimitIntervalSec=120` +
  `StartLimitBurst=3` bound crash loops.

**Test coverage.** ≈140 new/rewritten tests across 7 files:

- `tests/test_message_manager.py::TestDispatchCommand` — 8 tests
  covering command-handler dispatch, unknown action, missing
  payload/action, handler exceptions, mapping copy isolation, and
  regression checks for `type=message` and `type=config`.
- `tests/test_boot_config_endpoint.py` — 8 tests covering
  `HEROKU_SLUG_COMMIT` set, local git fallback, 401 without API key,
  401 with invalid API key, the v1 endpoint URL returning 404, one-
  shot publish at startup, no `on_connect_callback` kwarg, and
  publish failure handling.
- `tests/test_boot_config.py` — 26 tests covering the
  `BootConfig` dataclass, `from_response`, `fetch_boot_config` for
  the success/401/500/network/timeout/malformed/missing-key/empty/
  unparseable-url/custom-timeout scenarios, and `from_heroku_or_git`.
- `tests/test_status.py` — 15 tests covering `StatusSnapshot`,
  `StatusWriter` throttling + atomic writes + swallowed exceptions,
  and `read_status` defensive logic (missing, corrupt, schema
  mismatch, missing keys, stale mtime, wall-clock correctness).
- `tests/test_loader.py` (rewritten) — 30 tests covering env-var
  constants, atomic_swap, repo layout helpers, current_sha,
  `_build_exec_env` env-var pass-through, `exec_active`,
  `_is_status_healthy` probe, `fetch_expected_sha`, and the full
  `run_upgrade_flow` (Flask unreachable, local match, probe fails,
  stage raises, happy path, worktree already exists, all paths call
  exec_fn).
- `tests/test_app_handles_check_for_update.py` — 15 tests covering
  `_resolve_active_sha`, `_resolve_repo_dir`, `check_for_update`,
  and `_exec_into_loader` (no-op when active SHA missing/fetch
  fails/SHAs match; exec on mismatch; env vars + loader path
  correct; explicit `repo_dir=` kwarg respected).
- `tests/test_paho_mqtt_client.py` — 5 tests verifying the v2
  invariant that `PahoMqttClient` does NOT accept
  `on_connect_callback`.

**Removed.** v1 files (no longer present after the v2 refactor):

- `heart-matrix-controller/healthcheck.py`
- `tests/test_healthcheck.py`
- `tests/test_expected_sha_endpoint.py`
- `main.py --healthcheck` argparse flag
- `watch_subprocess` / 30s grace period in `loader.py`

**Out of scope.** No new third-party dependencies; stdlib only.
SQLite/S3 storage and the existing `/api/messages` webhook handler
are unchanged.
