## Why

The Raspberry Pi matrix controller is currently upgraded by hand (SSH, `git pull`, restart), and the Flask app is upgraded independently via `git push heroku main` with no shared version awareness and no safe rollback. The operator will not be physically present to recover from a bricked deploy, so we need push-button upgrades tied to Flask deploys with rollback safety — but the v1 design printed `sudo reboot` into the dispatcher and triggered a Pi reboot on every MQTT reconnect, which turned network flakiness into reboot storms. The v2 design replaces the reboot with a `check-for-update` command that the Pi's app handles by comparing its running SHA to Flask's, and only exchanging itself into the loader if they differ.

## What Changes

- New `heart-matrix-controller/loader.py` runs as the systemd `ExecStart`. On boot it queries Flask for the expected commit SHA, stages a new version into a git worktree if there's a mismatch, probes the staged version's `.status.json` to confirm it boots cleanly, atomically swaps a `current` symlink, and execs `main.py` with deployment env vars set. If anything fails along the way, it falls through to execing the existing `current/.../main.py` so the Pi can never brick itself.
- New `heart-matrix-controller/check_for_update.py` is registered as the `MessageManager` command handler for `action=check-for-update`. When Flask publishes the one-shot hint at startup, the Pi compares its `LINDSAY50_ACTIVE_SHA` env var to Flask's expected SHA. On mismatch it `os.execvpe`s into the loader; on match it does nothing. No reboot unless the SHAs actually differ.
- New `heart-matrix-controller/status.py` writes `$REPO_DIR/.status.json` from the render loop (throttled to one write per 3 seconds, atomically via `os.replace`). The loader probes the staged version by reading its status.json; a healthy report (mqtt connected, no last_error, recent tick) means the version is safe to swap.
- New `lib_shared/boot_config.py` holds the shared `BootConfig` dataclass + `fetch_boot_config()` + `from_heroku_or_git()`. Both Flask (server side) and the loader (Pi side) use the same HTTP + auth code.
- New `GET /api/sign/boot-config` endpoint on Flask returns `{"expected_sha": "<sha>"}` (renamed from the v1 `/api/sign/expected-sha`; the response shape is now just the SHA — no boot_id, no force_reboot). Auth via existing `X-API-Key`.
- New `type=command` MQTT envelope with `{"action": "check-for-update"}` payload. `MessageManager.dispatch` routes to a registered handler. Flask publishes this envelope **exactly once at startup** — the v1 `on_connect_callback` that re-published on every MQTT reconnect is removed.
- Systemd unit switches `ExecStart` to point at `loader.py`, adds `StartLimitIntervalSec=120` + `StartLimitBurst=3` to throttle crash loops.
- Pi filesystem layout: shared bare git repo (`.git/`) + per-SHA worktrees (`v-<sha>/`) + `current` symlink. Shared `.venv/`, `settings.toml`, `fonts/` stay outside per-version dirs.
- Drop the v1 `heart-matrix-controller/healthcheck.py` + `main.py --healthcheck` argparse flag — the status.json probe replaces them.
- Drop the v1 post-swap grace period (`watch_subprocess` + 30s rollback) — the status.json probe runs before the swap and is the single source of health truth.

## Capabilities

### New Capabilities

- `pi-upgrade-mechanism`: Loader process, blue/green layout (git worktrees + symlink swap), app-owned status.json probe for pre-swap validation, env-var contract (`LINDSAY50_ACTIVE_SHA` / `_REPO_DIR` / `_BOOT_ID`).
- `version-coordination`: Flask exposes expected SHA via authenticated admin endpoint; Pi queries it on boot and on the one-shot MQTT hint; rollback is the same `heroku rollback v123` operator workflow.
- `mqtt-command-envelope`: New `type=command` envelope routed by `MessageManager.dispatch` to a registered handler; initial handler is `check-for-update` (replaces the v1 `reboot` handler).

### Modified Capabilities

None. No existing spec requirements change — this is a net-new mechanism layered on top of existing message-envelope dispatch. The `type=message` and `type=config` dispatch branches are untouched.

## Impact

- **Files added:** `heart-matrix-controller/loader.py` (loader flow + status probe + atomic swap + exec), `heart-matrix-controller/check_for_update.py` (env-var-driven `action=check-for-update` handler), `heart-matrix-controller/status.py` (atomic writer + defensive reader), `lib_shared/boot_config.py` (shared SHA config + HTTP helper).
- **Files removed:** `heart-matrix-controller/healthcheck.py`, `main.py --healthcheck` argparse flag (replaced by status.json probe).
- **Files modified:** `heart-matrix-controller/main.py` (register the command handler; drop `--healthcheck`; construct `StatusWriter`), `heart-message-manager/main.py` (rename endpoint to `/api/sign/boot-config`; one-shot MQTT hint; drop the on_connect callback), `lib_shared/message_manager.py` (extend dispatch with `type=command` + registerable handlers), `lib_shared/paho_mqtt_client.py` (drop the `on_connect_callback` kwarg), `scripts/lindsay_50.service` + `scripts/startup_matrix_server.sh` (point at loader), `tests/` (rewrite for v2).
- **Operational impact:** Flask-only deploys (no code change) still trigger one MQTT envelope at startup. The Pi compares SHAs and only swaps if they differ, so Flask `config:set`-style restarts no longer cause redundant Pi reboots.
- **Repository layout:** One-time setup on the Pi — convert the existing clone to a bare repo + worktree layout. Documented in design.
- **No new third-party dependencies.** Uses `git worktree`, `os.replace`, `os.execvpe`, existing paho client.
