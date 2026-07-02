# Changelog

All notable changes to lindsay-50 are documented in this file.

## [Unreleased] — 2026-07-02

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
