## Context

The repo (`mbustosorg/lindsay-50`) deploys two halves from one codebase:

- **Flask app** (`heart-message-manager/`) → Heroku via `git push heroku main`. `Procfile` boots `gunicorn main:app`. Heroku auto-sets `HEROKU_SLUG_COMMIT` on every push. Publishes MQTT envelopes (`message`, `config`) via `lib_shared/paho_mqtt_client.py`.
- **Pi controller** (`heart-matrix-controller/`) → manually cloned onto a Pi 4, run via systemd (`scripts/lindsay_50.service` → `startup_matrix_server.sh` → `python3 main.py`). Subscribes to the same MQTT topic; `MessageManager.dispatch` (in `lib_shared/message_manager.py:338-351`) currently handles `type=message` and `type=config` only.

There is no shared version awareness and no automatic update path. Operator SSHs in to upgrade.

The change adds a self-upgrade mechanism on the Pi: on every boot (and on demand via MQTT), the Pi checks what version Flask is running, swaps itself over via blue/green, and rolls back if the new version fails. Flask becomes the source of truth (its `HEROKU_SLUG_COMMIT` is the expected version); rollback is `heroku rollback v123`.

The principal will not be physically present to recover from a bricked deploy, so the design must be self-healing under common failure modes.

## Goals / Non-Goals

**Goals:**
- `git push heroku main` (or any Flask restart) results in the Pi running the matching SHA on its next boot, or within ~30s if a reboot is triggered.
- A bad release does not leave the sign dark. The Pi stays on the last known-good version until a new version passes both pre-swap and post-swap checks.
- Rollback uses Heroku-native tooling — no Flask-side state to maintain or forget.
- The upgrade mechanism owns nothing about application health beyond "did the subprocess exit 0 from `--healthcheck`" and "did the subprocess stay up past the post-swap grace period". All other health checks live in the app and evolve independently.

**Non-Goals:**
- Operator-facing UI for triggering rollback/upgrade in v1 (operator uses `heroku rollback` / `mosquitto_pub`).
- Boot / status reporting from Pi back to Flask (deferred — separate project; metrics to report are captured in the proposal's "Out of scope").
- Spurious-reboot filtering on Flask-only deploys (deferred — every Flask restart triggers a Pi reboot; accepted trade-off).
- In-app health-check authoring — the initial check list lives in `healthcheck.py` and grows over time without loader changes.

## Decisions

### D1. Version source of truth = `HEROKU_SLUG_COMMIT`, no override endpoint

Flask reads `os.environ["HEROKU_SLUG_COMMIT"]` and exposes it at `GET /api/sign/expected-sha`. No DB column, no settings file, no override flag.

- **Why:** Zero state to maintain. `heroku rollback v123` already changes `HEROKU_SLUG_COMMIT` after the next dyno restart — so rollback works for free with no Flask code. Avoids the "operator forgot to clear the override" bug class.
- **Alternative considered:** `PUT /api/sign/expected-sha` operator override (stored in settings.toml / SQLite). Rejected for v1 to keep Flask stateless; can be added later without breaking the loader contract.

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

- **Why:** `git worktree add` against an existing repo is much faster than `git clone` — we already have the history. Symlink swap is atomic on the same filesystem. Old versions stay on disk as manual rollback targets.
- **Alternative considered:** Full `git clone` of each version into `v-<sha>/`. Rejected — wastes bandwidth on every upgrade and complicates credential management.
- **Alternative considered:** `rsync` or `tar` extract from a release artifact. Rejected — adds an artifact-build step and a non-git source of truth.

### D3. Loader is a separate process, not inlined into `main.py`

`loader.py` is the new systemd `ExecStart`. It owns the upgrade flow. `main.py` is invoked as a subprocess and is unaware of the loader.

- **Why:** If `main.py` itself is the broken thing, the loader can still reason about it (run `--healthcheck`, detect early subprocess exit, swap back). The loader is small, rarely changes, and shouldn't be impacted by app failures.
- **Alternative considered:** Put the upgrade logic in `main.py`'s startup before display init. Rejected — couples the loader's rollback logic to the broken code path.

### D4. App-owned health check via `main.py --healthcheck`

`main.py` accepts a `--healthcheck` argparse flag that runs the same init sequence (imports, `Display()` constructor, MQTT connect, `MessageManager.seed()`) and exits 0/non-0 without entering the render loop.

- **Why:** The loader only sees "did `--healthcheck` exit 0?" — it doesn't care what was checked. As we add checks (frame hash, MQTT message receipt, systemd watchdog), they go into the same function. The loader never changes.
- **Alternative considered:** The loader runs the checks itself by importing the modules. Rejected — duplicates init code, drift between health check and live init.

### D5. Post-swap grace period = 30s; subprocess exit in window → rollback

After swap + exec, the loader watches the subprocess for 30s. Any unexpected exit in that window triggers:
1. Swap `current` symlink back to previous known-good SHA.
2. `systemctl restart` (or just exit and let systemd restart).
3. Log the failure to Flask via MQTT publish (so Flask knows the rollback happened).

- **Why:** `--healthcheck` catches "won't even start" but not "starts then crashes 10s in". The grace period catches crashes that surface only after the render loop begins.
- **Alternative considered:** Systemd watchdog (`Type=notify`, `sd_notify`) for explicit readiness. More robust but more setup, and the 30s wall-clock check covers the common cases.
- **Alternative considered:** No grace period, trust `--healthcheck`. Rejected — too easy for "imports work but render loop crashes immediately" to brick the Pi.

### D6. Auto-reboot on Flask boot via MQTT command envelope

Flask publishes `{"type":"command","payload":{"action":"reboot"}}` on the same topic the Pi subscribes to, immediately after the paho client connects on Flask startup. Use the existing `publish_envelope()` path so QoS 1 + PUBACK semantics are preserved.

- **Why:** Forces Flask and Pi to stay in sync. Any Flask restart (new deploy, dyno cycle, manual restart, `heroku config:set`) triggers a Pi reboot shortly after — which then self-upgrades if the SHA changed.
- **Alternative considered:** Track previous slug commit in Flask; only publish reboot if it changed. Rejected — adds state, requires a "first boot" sentinel, and config-only Heroku operations already cause spurious Flask dyno restarts that we don't want to filter out.
- **Trade-off:** `heroku config:set` (no code change) triggers a Flask dyno restart, which triggers a Pi reboot. Pi reboots onto the same SHA — no swap, ~10s of dark panel, then back. Acceptable for v1.

### D7. New `type=command` envelope, single `action=reboot` handler

Extend `MessageEnvelope` with `type=command`. Dispatch in `MessageManager.dispatch` adds one branch:

```python
elif envelope.type == "command":
    handle_command(envelope.payload)
```

Where `handle_command({"action": "reboot"})` runs `os.system("sudo reboot")`. Unknown actions logged and dropped (matches existing pattern for unknown envelope types).

- **Why:** Clean separation from message/config payloads. The dispatcher is the single dispatch point; adding more command actions later (`update_check`, `restart_service`, etc.) is incremental.
- **Alternative considered:** Reuse `type=config` with a `reboot_required: true` field. Rejected — mixes deployment concerns with display config.

### D8. Systemd throttling, not loader-managed

Systemd unit adds `StartLimitIntervalSec=120` + `StartLimitBurst=3`. After 3 service exits in 2 minutes, systemd stops restarting. The operator can SSH in to diagnose.

- **Why:** Cheap defense-in-depth. If the loader itself has a bug that causes tight crash loops, systemd bounds the damage.
- **Alternative considered:** Loader tracks its own restart count. Rejected — systemd already does this correctly.

## Risks / Trade-offs

- **Dirty working tree blocks `git worktree add`.** → Loader calls `git -C <old_dir> reset --hard <local_sha>` before staging. Acceptable: the operator should not be editing files on the Pi.
- **Network down at boot fails `git worktree add`.** → Catch the exception, log to Flask via MQTT, continue booting the old version. Retry on next boot.
- **Symlink swap interrupted by power loss mid-update.** → `ln -sfn` on the same filesystem is atomic; worst case is "swap didn't happen, sign runs old version". The new worktree dir is left on disk for inspection.
- **Health check passes but render crashes 5s in.** → 30s post-swap grace period detects and rolls back. Tradeoff: 30s of bad display per failed upgrade.
- **Flask publishes reboot on startup, but Pi is mid-render-loop.** → Pi receives the command mid-cycle, reboots within ~2s. Acceptable — reboot is the upgrade trigger, this is by design.
- **Concurrent Flask boots (rolling deploy).** → Each Flask instance publishes a reboot command on startup. Pi gets N copies in quick succession, only the first has effect (subsequent ones reboot the Pi onto the same SHA again). Idempotent. Acceptable.
- **Old `v-<sha>/` directories accumulate.** → Follow-up: GC policy (keep last N versions, or delete versions older than X days). Out of scope for v1.
- **`sudo reboot` requires passwordless sudo.** → Documented in deployment README; the systemd unit already runs as root so this is a one-time `visudo` change.
- **Pi's `main.py` was previously the systemd entrypoint.** → Migrating to `loader.py` means existing systemd units need to be updated on the Pi. One-time operator action; documented in the tasks.

## Migration Plan

1. **Code lands on `main`:** All Python + systemd unit changes merged.
2. **One-time Pi bootstrap (manual, ~5 min):**
   - SSH into Pi.
   - `cd /home/pi/projects/lindsay-50 && git pull` (last manual pull).
   - Convert existing clone to bare repo: `mv .git .git.tmp && git clone --bare .git.tmp .git && rm -rf .git.tmp`.
   - Create the first worktree from current HEAD: `git worktree add v-<current_sha> HEAD` and `ln -sfn v-<current_sha> current`.
   - Move `settings.toml`, `fonts/`, `.venv/` so they sit at the repo root, not inside the worktree. (They likely already do.)
   - Update systemd unit: `sudo cp scripts/lindsay_50.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart lindsay_50`.
   - Verify: `systemctl status lindsay_50`, `journalctl -u lindsay_50 -f`, panel comes up displaying messages.
3. **First deploy via Flask:** `git push heroku main`. Flask restarts → publishes reboot → Pi reboots → loader queries `/api/sign/expected-sha` → SHA matches → skip swap → boot old version (which is now the new SHA, since we just deployed). Sign comes up clean.
4. **First rollback test:** `heroku rollback v123`. Flask dyno restarts → publishes reboot → Pi reboots → loader detects mismatch → pulls the older SHA → swaps → restarts.
5. **Operator manual rollback (post-deploy):** SSH in, edit `current` symlink: `ln -sfn v-<old_sha> current && sudo systemctl restart lindsay_50`. Works because old worktrees are kept on disk.

## Open Questions

- **GC of old worktrees:** how many `v-<sha>/` directories to retain? Configurable constant in `loader.py`?
- **`sudo reboot` vs `systemctl restart lindsay_50`:** reboot is heavier (panel goes dark, systemd cycle); restart is lighter but doesn't apply upgrades. Use reboot for `action=reboot` (matches the user's mental model of "reboot = pull new code"), use restart only for the loader's own crash recovery.
- **`HEROKU_SLUG_COMMIT` is not always set in local dev.** When running Flask locally (`python heart-message-manager/main.py`), the endpoint should fall back to `git rev-parse HEAD` from the local repo. Local dev doesn't need this mechanism but it should not 500.
- **Systemd unit's `Environment=LOG_LEVEL=...` and `PYTHONPATH=...`:** currently set in `startup_matrix_server.sh`. Need to either inline into the unit or keep the shell wrapper. Keeping the wrapper is the path of least change.