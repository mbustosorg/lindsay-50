# Changelog

All notable changes to lindsay-50 are documented in this file.

## [Unreleased] — 2026-07-02

### Changed — Senders allowlist + filter/sender lifecycle (issue #6, `openspec_change_name: implement-senders-filtering`)

The `/settings` "Allowed Senders" panel (which iterated the non-existent
`cfg.allowed_senders` and rendered an empty list) is replaced by a working
**Senders** panel. Each sender entry now carries two independent fields:

- **Action** (`allow` / `suppress`) — the effect when the sender matches.
- **Status** (`enabled` / `disabled`) — the lifecycle flag: mute an entry
  without deleting it (keeps the phone→name mapping for display).

Filtering is **allowlist-by-default and egress-only**: a message renders
only if its sender's entry is `action="allow"` AND `status="enabled"`. Every
inbound SMS is still stored on ingress (SQLite + S3 + MQTT), so adding a
sender later un-suppresses their already-received messages on the next config
update — no Twilio re-fetch.

`FilterRule` gained a `status` (`enabled` / `disabled`) lifecycle field
(same "disable it vs. delete it" affordance for keyword/regex/message rules),
and the `sender` rule **type was removed** — sender matching is now solely the
Senders list's job. Phone numbers are normalized to `+1XXXXXXXXXX` (last-10-
digits, formatting-insensitive) via the new `lib_shared/phone_utils.py`.

Config schema bumps to **version 3**. A `_v2_to_v3` migration runs on server
startup and on every device `update_from_dict`: it renames each sender's v2
`status` (`allowed`/`blocked`) to `action` (`allow`/`suppress`), adds the new
`status="enabled"` lifecycle field, renames `FilterRule.enabled` (bool) to
`status` (enum), and converts stored `type=sender` rules into Senders entries
(`action="suppress"`, `status="enabled"`).

**Operator action required after upgrade:** filtering is now an allowlist, so
senders NOT in the list are suppressed (previously they displayed with no
name). Add each known sender to the Senders table with **Action = Allow** and
**Status = Enabled** to restore their visibility. The migration does NOT
auto-add senders from message history. Review any entries created from
migrated `type=sender` rules — they arrive as `Suppress` and can be deleted or
flipped to `Allow`.

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
