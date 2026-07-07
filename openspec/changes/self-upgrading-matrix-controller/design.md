## Context

The repo (`mbustosorg/lindsay-50`) deploys two halves from one codebase:

- **Flask app** (`heart-message-manager/`) → Heroku via `git push heroku main`. `Procfile` boots `gunicorn main:app`. Heroku auto-sets `HEROKU_SLUG_COMMIT` on every push. Publishes MQTT envelopes (`message`, `config`) via `lib_shared/paho_mqtt_client.py`.
- **Pi controller** (`heart-matrix-controller/`) → manually cloned onto a Pi 4, run via systemd (`scripts/lindsay_50.service` → `startup_matrix_server.sh` → `python3 main.py`). Subscribes to the same MQTT topic; `MessageManager.dispatch` (in `lib_shared/message_manager.py`) currently handles `type=message` and `type=config` only.

There is no shared version awareness and no automatic update path. Operator SSHs in to upgrade.

The v2 change adds a self-upgrade mechanism on the Pi: on every boot the loader checks what version Flask is running, swaps itself over via blue/green if the SHAs differ, and probes the staged version via a `.status.json` file before committing to the swap. An MQTT-published `check-for-update` command lets a running Pi notice its SHA has gone stale and re-enter the loader without a full reboot.

Flask remains the source of truth (its `HEROKU_SLUG_COMMIT` is the expected version); rollback is `heroku rollback v123`.

The principal will not be physically present to recover from a bricked deploy, so the design must be self-healing under common failure modes.

The v2 design supersedes the v1 design that called for `action=reboot` + `on_connect_callback` re-publish + post-swap grace period. Review feedback identified three concrete failure modes in v1:

1. **`on_connect_callback` re-publish turns network blips into reboot storms.** Flask's paho client reconnects ~once per session on bad networks; v1 published the reboot envelope on each reconnect, restarting the Pi each time. v2 publishes once at startup instead.
2. **`action=reboot` is the wrong abstraction.** Rebooting the Pi to swap versions is wasteful when the SHAs may already match (e.g., on Flask `config:set`-style restarts). v2 uses `action=check-for-update` and only re-execs the loader if SHAs differ.
3. **`--healthcheck + 30s grace period` has a 30s downtime per failed upgrade.** v2 probes the staged version's `.status.json` *before* swap, in the loader process — failures abort the swap with zero downtime on the running version.

## Goals / Non-Goals

**Goals:**
- `git push heroku main` results in the Pi running the matching SHA on its next boot, or within ~10s if the one-shot `check-for-update` envelope arrives while the Pi is running.
- A bad release does not leave the sign dark. The Pi stays on the last known-good version until a new version passes the pre-swap status.json probe.
- Flask restarts that don't change `HEROKU_SLUG_COMMIT` (config:set, dyno cycle) do NOT restart the Pi.
- Rollback uses Heroku-native tooling — no Flask-side state to maintain or forget.
- The upgrade mechanism owns nothing about application health beyond "is `.status.json` healthy?". All other health checks live in the app and evolve independently.

**Non-Goals:**
- Operator-facing UI for triggering rollback/upgrade in v2 (operator uses `heroku rollback`).
- Boot / status reporting from Pi back to Flask (deferred — separate project).
- Automatic git LFS / submodule management on the Pi — the repo is plain Python.
- In-app health-check authoring beyond the few fields `StatusSnapshot` exposes — the loader only looks at `mqtt_connected`, `last_tick_age_ms`, and `last_error`.

## Decisions

### D1. Version source of truth = `HEROKU_SLUG_COMMIT`, no override endpoint

Flask reads `os.environ["HEROKU_SLUG_COMMIT"]` and exposes it at `GET /api/sign/boot-config`. No DB column, no settings file, no override flag.

- **Why:** Zero state to maintain. `heroku rollback v123` already changes `HEROKU_SLUG_COMMIT` after the next dyno restart — so rollback works for free with no Flask code. Avoids the "operator forgot to clear the override" bug class.
- **Alternative considered:** `PUT /api/sign/expected-sha` operator override (stored in settings.toml / SQLite). Rejected for v1 to keep Flask stateless; deferred in v2 since the v2 endpoint is even simpler.

### D2. Blue/green via shared bare repo + git worktrees

```
/home/pi/projects/lindsay-50/
├── .git/                    # bare repo (cheap worktrees, no re-download)
├── current -> v-<sha>       # symlink: active version
├── v-<sha>/                 # per-version worktree
├── .venv/                   # shared (already the convention)
├── settings.toml            # shared (outside per-version dirs)
└── fonts/                   # shared
```

- **Why:** `git worktree add` against an existing repo is much faster than `git clone`. Symlink swap is atomic on the same filesystem. Old versions stay on disk as manual rollback targets.
- **Alternative considered:** Full `git clone` of each version into `v-<sha>/`. Rejected — wastes bandwidth on every upgrade and complicates credential management.
- **Alternative considered:** `rsync` or `tar` extract from a release artifact. Rejected — adds an artifact-build step and a non-git source of truth.

### D3. Loader is a separate process, not inlined into `main.py`

`loader.py` is the new systemd `ExecStart`. It owns the upgrade flow. `main.py` is invoked as a subprocess and is unaware of the loader.

- **Why:** If `main.py` itself is the broken thing, the loader can still reason about it (probe `.status.json`, detect early subprocess exit, swap back). The loader is small, rarely changes, and shouldn't be impacted by app failures.
- **Alternative considered:** Put the upgrade logic in `main.py`'s startup before display init. Rejected — couples the loader's rollback logic to the broken code path.

### D4. App-owned `.status.json` probe (replaces v1 `--healthcheck`)

`heart-matrix-controller/status.py` writes a `.status.json` file from the render loop every 3 seconds. The loader's pre-swap probe spawns `v-<sha>/main.py`, waits up to a small budget for the file to report `mqtt_connected=true` with no `last_error`, then kills the subprocess.

- **Why:** The loader only sees "did `.status.json` become healthy?" — it doesn't care what was checked. As the app adds checks (heartbeat frequency, message receipt, sd_notify), they go into `StatusSnapshot` fields, not the loader. The probe runs IN the loader process, so failures abort the swap with zero downtime on the running version.
- **Alternative considered (v1):** `main.py --healthcheck` argparse flag + subprocess exit code. Replaced because the subprocess `os.execvp` was incompatible with exec'ing into the SAME process for the probe — there was no way to read the status.json *after* the subprocess finished cleanly without a state that survived the exec.
- **Alternative considered:** The loader runs the checks itself by importing the modules. Rejected — duplicates init code, drift between probe and live init, and importing doesn't actually exercise the rgbmatrix / MQTT stack.

### D5. No post-swap grace period

The v1 30s `watch_subprocess` rollback is dropped. The status.json probe (D4) is the single source of health truth and it runs BEFORE the swap. After swap, `os.execvpe` into the new `main.py` directly — if it crashes immediately, the loader is gone too, and systemd's `StartLimitBurst=3` (D8) bounds the damage.

- **Why:** A 30s window where the running subprocess can fail and trigger a rollback is wasted work; the staged version already proved itself with the probe. Removing the grace period removes the only piece of code that ran during "the new version is on screen" — the only window where a rollback can cause flicker.
- **Alternative considered (v1):** `watch_subprocess(proc, repo_dir, previous_sha, grace_seconds=30)`. Replaced by D4's pre-swap probe.
- **Alternative considered:** Systemd watchdog (`Type=notify`, `sd_notify`) for post-swap readiness. Deferred — the status.json probe catches the common failure modes during staging.

### D6. One-shot MQTT hint at Flask startup, NOT on reconnect

Flask publishes `{"type":"command","payload":{"action":"check-for-update"}}` on the same topic the Pi subscribes to, exactly once at Flask startup. The paho client's `on_connect_callback` parameter is removed from `PahoMqttClient.__init__`.

- **Why:** Forces Flask and the Pi to stay in sync at deploy time without the v1 failure mode where network reconnects published additional hints. The "is this the first connect?" check is simpler than tracking reconnect counts.
- **Alternative considered (v1):** Publish in `on_connect_callback` for every connect. Replaced because paho reconnects several times on bad networks, multiplying the hint.
- **Alternative considered:** Track previous slug commit in Flask and only publish the hint on change. Rejected — adds state, and v2's `check-for-update` action is cheap enough (one HTTP call) that publishing always is fine.

### D7. `action=check-for-update` instead of `action=reboot`

The default `MessageManager` command handler for `action=check-for-update` lives in `heart-matrix-controller/check_for_update.py`. It compares the local `LINDSAY50_ACTIVE_SHA` env var to Flask's expected SHA and `os.execvpe`s into the loader on mismatch; on match it does nothing.

- **Why:** Decoupling "swap versions" from "reboot the Pi" lets us avoid restarting systemd, the panel, and the rgbmatrix driver when the versions already match. Most Flask restarts (config:set, dyno cycle) don't change `HEROKU_SLUG_COMMIT`, so they shouldn't restart the Pi.
- **Alternative considered (v1):** `os.system("sudo reboot")` inside `handle_command`. Replaced because reboot is heavier than necessary when a targeted `os.execvpe` into the loader suffices, and because reboot-then-reboot-when-loader-decides is wasteful.
- **Alternative considered:** Two-step: `action=reboot` rebuilds systemd, plus a separate `action=swap-if-stale`. Rejected — one-step is simpler and the loader is idempotent.

### D8. Systemd throttling, not loader-managed

Systemd unit adds `StartLimitIntervalSec=120` + `StartLimitBurst=3`. After 3 service exits in 2 minutes, systemd stops restarting. The operator can SSH in to diagnose.

- **Why:** Cheap defense-in-depth. If the loader itself has a bug that causes tight crash loops, systemd bounds the damage.
- **Alternative considered:** Loader tracks its own restart count. Rejected — systemd already does this correctly.

### D9. Env-var contract: `LINDSAY50_REPO_DIR` / `_ACTIVE_SHA`

Two env vars flow between loader and app via `os.execvpe`'s env dict:

- `LINDSAY50_REPO_DIR` — set once by `scripts/setup-pi.sh` (or systemd `Environment=`); both loader and `check_for_update` read it. Fallback default `/home/pi/projects/lindsay-50`.
- `LINDSAY50_ACTIVE_SHA` — set by `check_for_update.check_for_update` BEFORE `os.execvpe`-ing into the loader, so the loader knows the SHA of the running app that called it. The loader sets it on the env passed to the next `main.py` invocation.

- **Why:** A single exec is cheaper than a fork/exec and survives across the loader/app boundary as a tuple of env vars. The alternative (reading from disk) is slower and requires a separate storage location.

### D10. Shared `lib_shared/boot_config.py`

`BootConfig` dataclass + `fetch_boot_config(api_url, api_key, *, timeout_s=5.0)` + `from_heroku_or_git()` live in `lib_shared/boot_config.py` and are imported by both Flask (server side) and the loader (Pi side).

- **Why:** HTTP + auth + JSON parsing for the Flask API lives in one place. Tests cover the contract once. Flask keeps the v1 local-dev fallback (`HEROKU_SLUG_COMMIT` unset → `git rev-parse HEAD`) by delegating to `from_heroku_or_git()`.
- **Alternative considered:** Each side parses the response its own way. Rejected — JSON deserialization drift between two callers is a maintenance hazard.

## Risks / Trade-offs

- **Dirty working tree blocks `git worktree add`.** → Loader calls `git -C <old_dir> reset --hard <local_sha>` before staging. Acceptable: the operator should not be editing files on the Pi.
- **Network down at boot fails `git worktree add`.** → Catch the exception, log to Flask via MQTT publish, continue booting the old version. Retry on next boot or next `check-for-update` hint.
- **Symlink swap interrupted by power loss mid-update.** → `ln -sfn` on the same filesystem is atomic; worst case is "swap didn't happen, sign runs old version". The new worktree dir is left on disk for inspection.
- **(v1 risk REMOVED) Health check passes but render crashes 5s in.** → Pre-swap status.json probe runs against the staged version in isolation; failures abort before swap. Removed the 30s grace period and the runtime crash-rollback loop entirely.
- **Status.json probe stalls (staged app never reports `mqtt_connected=true`).** → Loader kills the probe subprocess on a timeout (default 15s) and aborts the swap with a logged warning. The staged worktree is left on disk for inspection.
- **Pi doesn't receive the `check-for-update` envelope (broker down).** → Loader already verifies on its own boot; the MQTT hint is best-effort. Worst case is a stale version until the next natural reboot or until the broker recovers.
- **`LINDSAY50_ACTIVE_SHA` unset on first boot after `setup-pi.sh`.** → `check_for_update` is a no-op if the env var is missing; the loader runs end-to-end during boot anyway. Operator never sees a "first boot stuck on stale version" failure mode.
- **Pi's `main.py` was previously the systemd entrypoint.** → Migrating to `loader.py` means existing systemd units need to be updated on the Pi. One-time operator action; documented in the migration plan.

## Migration Plan

1. **Code lands on `main`:** All Python + systemd unit changes merged.
2. **One-time Pi bootstrap (manual, ~5 min):**
   - SSH into Pi.
   - `cd /home/pi/projects/lindsay-50 && git pull` (last manual pull).
   - Convert existing clone to bare repo: `mv .git .git.tmp && git clone --bare .git.tmp .git && rm -rf .git.tmp`.
   - Create the first worktree from current HEAD: `git worktree add v-<current_sha> HEAD` and `ln -sfn v-<current_sha> current`.
   - Move `settings.toml`, `fonts/`, `.venv/` so they sit at the repo root, not inside the worktree. (They likely already do.)
   - Set `LINDSAY50_REPO_DIR=/home/pi/projects/lindsay-50` in `/etc/default/lindsay-50` or systemd's `Environment=`.
   - Update systemd unit: `sudo cp scripts/lindsay_50.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart lindsay_50`.
   - Verify: `systemctl status lindsay_50`, `journalctl -u lindsay_50 -f`, panel comes up displaying messages.
3. **First deploy via Flask:** `git push heroku main`. Flask restarts → publishes one `check-for-update` envelope → Pi's running app compares SHAs (assume mismatch), execs into the loader → loader stages, probes, swaps, execs the new `main.py`. Sign comes up clean. (If SHAs happen to match, no swap, no visible downtime.)
4. **First rollback test:** `heroku rollback v123`. Flask dyno restarts → publishes `check-for-update` → Pi's running app sees the mismatch and execs into the loader → loader pulls the older SHA → swaps → restarts.
5. **Operator manual rollback (post-deploy):** SSH in, edit `current` symlink: `ln -sfn v-<old_sha> current && sudo systemctl restart lindsay_50`. Works because old worktrees are kept on disk.

## Open Questions

- **GC of old worktrees:** how many `v-<sha>/` directories to retain? Configurable constant in `loader.py`?
