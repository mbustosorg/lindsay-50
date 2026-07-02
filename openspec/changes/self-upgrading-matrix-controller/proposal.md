## Why

The Raspberry Pi matrix controller is currently upgraded by hand (SSH, `git pull`, restart), and the Flask app is upgraded independently via `git push heroku main` with no shared version awareness and no safe rollback. The operator will not be physically present to recover from a bricked deploy, so we need push-button upgrades tied to Flask deploys with rollback safety.

## What Changes

- New `heart-matrix-controller/loader.py` runs as the systemd `ExecStart`. On every boot it queries Flask for the expected commit SHA, stages a new version into a git worktree if there's a mismatch, runs the app's health check, atomically swaps a `current` symlink, and execs `main.py` as a subprocess. If the subprocess exits within 30s of starting, it rolls `current` back to the previous known-good SHA and restarts.
- New `main.py --healthcheck` flag runs the same init sequence (imports, display init, MQTT connect, REST seed) and exits 0/1 without entering the render loop. The upgrade process invokes this against the staged version before swapping — the loader never needs to know what the health check verifies, only its exit code.
- New `GET /api/sign/expected-sha` endpoint on Flask returns `HEROKU_SLUG_COMMIT`. Auth via existing `X-API-Key`.
- New `type=command` MQTT envelope with `{action: "reboot"}` payload. `MessageManager.dispatch` routes to a command handler that runs `sudo reboot`.
- Flask publishes a `command=reboot` envelope on startup (after MQTT connects) so any Flask restart (new deploy, dyno cycle, `heroku config:set`) triggers a Pi reboot shortly after, which then self-upgrades if needed.
- Systemd unit switches `ExecStart` to point at `loader.py`, adds `StartLimitIntervalSec=120` + `StartLimitBurst=3` to throttle crash loops.
- Pi filesystem layout: shared bare git repo (`.git/`) + per-SHA worktrees (`v-<sha>/`) + `current` symlink. Shared `.venv/`, `settings.toml`, `fonts/` stay outside per-version dirs.

## Capabilities

### New Capabilities

- `pi-upgrade-mechanism`: Loader process, blue/green layout (git worktrees + symlink swap), atomic rollback on early subprocess exit, app-owned health check invocation.
- `version-coordination`: Flask exposes expected SHA via authenticated admin endpoint; Pi queries it on boot and self-upgrades on mismatch; Flask publishes a reboot command on its own startup to force sync with the Pi.
- `mqtt-command-envelope`: New `type=command` envelope routed by `MessageManager.dispatch` to a command handler; initial handler runs `sudo reboot` for `action=reboot`.

### Modified Capabilities

None. No existing spec requirements change — this is a net-new mechanism layered on top of existing message-envelope dispatch.

## Impact

- **Files added:** `heart-matrix-controller/loader.py`, `heart-matrix-controller/healthcheck.py`.
- **Files modified:** `heart-matrix-controller/main.py` (add `--healthcheck` flag; route `type=command` to handler), `heart-message-manager/main.py` (add `/api/sign/expected-sha`; publish reboot on startup), `lib_shared/message_manager.py` (extend `dispatch` with `type=command`), `scripts/lindsay_50.service` (point `ExecStart` at loader; add restart throttling).
- **Operational impact:** Every Flask deploy (including Flask-only deploys and `heroku config:set` dyno restarts) triggers a Pi reboot. The Pi's self-upgrade then either swaps to the new version (if different SHA) or restarts in place (if same SHA).
- **Repository layout:** One-time setup on the Pi — convert the existing clone to a bare repo + worktree layout. Documented in design.
- **No new third-party dependencies.** Uses `git worktree`, existing `subprocess`/`os` for `sudo reboot`, existing paho client.