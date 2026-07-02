# Changelog

All notable changes to lindsay-50 are documented in this file.

## [Unreleased] — 2026-07-02

### Added — Self-upgrading Pi matrix controller (issue #49)

The Pi matrix controller now upgrades itself whenever Flask restarts with a
new commit. The Pi is no longer a thing you have to `ssh` into to update.

**Flow.** When Flask restarts (e.g. `git push heroku main`), it now
publishes a one-shot MQTT `command=reboot` envelope after its MQTT client
reconnects. The Pi reboots, runs `loader.py` as the systemd unit's
`ExecStart`, which queries `GET /api/sign/expected-sha` on Flask, stages
the new commit into `git worktree add v-<sha>`, runs `main.py --healthcheck`
on the staged worktree, and atomically swaps the `current` symlink. The
loader then `os.execvp`s the new version so systemd sees `main.py` as the
direct child and signal handling is preserved.

**Rollback.** If the new subprocess exits non-zero within a 30-second grace
period, the loader swaps `current` back to the previous known-good worktree
and re-execs. Old worktrees stay on disk after every upgrade, so manual
rollback is also just `ln -sfn v-<old-sha> current` followed by a service
restart.

**Operator workflow changes.**

- `heroku rollback v123` is now the rollback primitive — it sets
  `HEROKU_SLUG_COMMIT` to v123's hash, Flask restarts, publishes
  `command=reboot`, and the Pi pulls v123 on its own.
- One-time Pi bootstrap (~1 minute of downtime): `sudo systemctl stop
  lindsay_50` → `git pull` → `sudo scripts/setup-pi.sh`. The script is
  idempotent and converts the existing clone to a bare-repo + per-SHA
  worktrees + `current` symlink layout.

**New files.**

- `heart-matrix-controller/loader.py` — the upgrade orchestrator
  (query → stage → health-check → swap → exec → watch).
- `heart-matrix-controller/healthcheck.py` — `run_healthcheck()` runs
  display, MQTT, and REST seed checks; `main.py --healthcheck` calls
  it and exits 0/1.
- `scripts/setup-pi.sh` — one-time Pi bootstrap (bare repo + worktree
  + symlink).

**Modified files.**

- `lib_shared/message_manager.py` — `dispatch()` now routes
  `type=command` envelopes; `action=reboot` runs `sudo reboot`,
  unknown actions are logged and dropped.
- `lib_shared/paho_mqtt_client.py` — added `on_connect_callback` so
  Flask can publish a one-shot reboot envelope after each successful
  MQTT connect.
- `heart-message-manager/main.py` — added `GET /api/sign/expected-sha`
  (auth-gated, with `HEROKU_SLUG_COMMIT` env var + local `git
  rev-parse HEAD` fallback); wired the reboot-envelope publish into
  the paho client's `on_connect_callback`.
- `heart-matrix-controller/main.py` — `--healthcheck` argparse
  short-circuit at the top of the module so the loader can probe a
  staged worktree without paying the cost of the full rgbmatrix
  import.
- `scripts/lindsay_50.service` — `ExecStart` still calls
  `startup_matrix_server.sh` (which now exec's `loader.py` instead
  of `main.py`); added `StartLimitIntervalSec=120` +
  `StartLimitBurst=3` to bound crash loops.
- `scripts/startup_matrix_server.sh` — exec's
  `$REPO_DIR/current/heart-matrix-controller/loader.py` so systemd
  signals are handed off correctly.

**Test coverage.** 61 new tests across 5 files:

- `tests/test_message_manager.py::TestDispatchCommand` — 8 tests
  covering reboot dispatch, unknown action, missing payload/action,
  and regression checks for `type=message` and `type=config`.
- `tests/test_expected_sha_endpoint.py` — 8 tests covering
  `HEROKU_SLUG_COMMIT` set, 401 without API key, 401 with invalid
  API key, git fallback, callback wiring, and reboot publish
  failure handling.
- `tests/test_healthcheck.py` — 11 tests covering the success path,
  display failure, MQTT unreachable, seed failure, corrupt seed
  data, and `main.py --healthcheck` exit codes.
- `tests/test_loader.py` — 17 tests covering atomic swap, Flask
  unreachable, full upgrade flow against a real bare-repo fixture
  with two commits, watch_subprocess rollback to `v-<previous_sha>`
  on early non-zero exit, no rollback past the grace period, no
  rollback on clean exit, and repo layout helpers.
- `tests/test_systemd_unit.py` — 17 tests covering `ExecStart`
  invoking the startup wrapper, the wrapper exec'ing `loader.py`,
  `WorkingDirectory` at repo root, `StartLimitIntervalSec=120`,
  `StartLimitBurst=3`, `Restart=always`, `User=root`,
  `After=network-online.target`, `setup-pi.sh` executable bit, the
  bare-repo conversion, first-worktree creation, `current` symlink,
  idempotency, systemd reload, and env var preservation.

**Out of scope.** No new third-party dependencies; stdlib only.
SQLite/S3 storage and the existing `/api/messages` webhook handler
are unchanged.
