## Context

The lindsay-50 system has two halves deployed from one codebase:

- **Flask app** (`heart-message-manager/`) → Heroku via `git push heroku main`. Operator upgrades are tied to Flask deploys and `HEROKU_SLUG_COMMIT` is the source of truth for the Flask version.
- **Pi controller** (`heart-matrix-controller/`) → runs on a Raspberry Pi 4 via systemd. Subscribes to the same MQTT topic as Flask. Runs `loader.py` (per `self-upgrading-matrix-controller`) which stages new versions, probes via `.status.json`, and atomically swaps a `current -> v-<sha>/` symlink. The Pi publishes a `StatusSnapshot` over MQTT (per the in-flight `add-sign-status-reports` change) on a 30s cadence.

Today's Pi upgrade control is binary: auto-upgrade triggers on Flask deploys (via the loader + one-shot `check-for-update` MQTT hint), or the operator SSHes in by hand. There is no admin UI for:

1. **Pinning the Pi to a specific version.** If a Flask deploy is bad but the operator doesn't want to roll it back at the Flask level, they have no way to keep the Pi on the previous version short of disabling auto-upgrade in code and redeploying.
2. **Per-Pi override of auto-upgrade.** If the Pi discovers — through something its loader cannot detect (e.g. a render-pattern regression that boots and probes cleanly but breaks display quality) — that it shouldn't auto-update, it has no way to tell Flask "stop pushing the target at me".
3. **Issuing recovery commands (`force-upgrade`, `restart`, `shutdown`) without SSH.** Network is unreliable, the operator is often remote, and SSH isn't always available.

This change adds an operator-facing Settings UI section + three MQTT-dispatched commands on top of the existing loader + status mechanisms. The UI consumes version data already flowing through the `add-sign-status-reports` pipeline; the commands ride the existing `type=command` envelope introduced in `self-upgrading-matrix-controller`.

The principal will not be physically present to recover from a bricked deploy. Design must be self-healing and never take down a working sign.

## Goals / Non-Goals

**Goals:**

- The Settings page exposes three version fields (Flask, Pi, Target Pi) and two auto-update toggles (Flask-side and Pi-side) so the operator can pin, override, and inspect from the admin UI alone.
- The Settings page exposes three buttons (Force upgrade, Restart, Shutdown) that publish one-shot MQTT command envelopes; the Pi handles them with no SSH required.
- Settings changes (target SHA, auto-update flag toggles) take effect without Flask restart; the loader sees them on its next boot or via the existing one-shot hint.
- The upgrade decision becomes: "Pi upgrades IFF target ≠ active AND flask-auto-update is on AND pi-auto-update is on." Either flag off → hold current version. Force-upgrade bypasses both.
- All three new commands are routed through the loader's existing fallthrough — a failure in the handler leaves the Pi running on its current version, never bricks it.
- Settings persist on the Flask side (SQLite via `SignConfig`); the Pi auto-update flag is reported as Pi-side state via the status flow but is NOT persisted as a hard brake on the Pi.

**Non-Goals:**

- A MQTT events topic for discrete log entries (parked per `add-sign-status-reports`).
- Pi-side persistent version pinning (the issue explicitly defers this — "We may add the ability to try to pin it to a specific version in the future, if needed"). Today the Pin is a server-side `target_pi_sha` field; the Pi always trusts whatever the most-recent Flask-published target was.
- A Flask-side rollback UI (the existing `heroku rollback v123` operator workflow remains the rollback path).
- Per-pattern health checks beyond `.status.json`; the loader's existing `mqtt_connected`, `last_tick_age_ms`, `last_error` probe stays the source of truth for "is the new version safe to swap to?".

## Decisions

### D1. Three actions, one envelope type, `MessageManager.dispatch`

The three new commands ride the existing `type=command` envelope + `MessageManager.dispatch` routing introduced in `self-upgrading-matrix-controller`. The action strings are:

- `force-upgrade` — handler in `heart-matrix-controller/command_handlers.py:force_upgrade`. Calls `os.execvpe(sys.executable, [sys.argv[0], *sys.argv[1:]], env)` where `sys.argv[0]` is set to `<repo_dir>/loader.py` and `LINDSAY50_REPO_DIR` is on env. The loader then runs the same SHA-check + stage + probe + swap (or no-op) as on boot.
- `restart` — handler calls `subprocess.run(["sudo", "reboot"], check=False)`. The loader (systemd ExecStart) restarts. Loader reads settings on boot.
- `shutdown` — handler calls `subprocess.run(["sudo", "shutdown", "-h", "now"], check=False)`. Pi halts; loader handles reboot if/when it comes back.

Each command envelope is published exactly once on click; no retries, no debouncing. The Flask endpoint just publishes and returns 202.

- **Why:** Reuses an existing, tested routing path. No new envelope type, no new dispatch logic — just three new entries in the handler registry that `main.py:register_command_handlers` adds at boot.
- **Alternative considered:** A separate `type=upgrade-control` envelope. Rejected — it's the same shape as `type=command`, just with different actions. Splitting the topic would force operators to reason about which command-path a setting-change rides on.
- **Alternative considered:** HTTP POST from Flask to a Pi-side webhook endpoint. Rejected — there's already MQTT plumbing; HTTP would be a parallel path.

### D2. Settings persistence: a new `PiUpgradeSettings` model in `lib_shared/models.py`, SQLite-backed

`PiUpgradeSettings` is a new dataclass (sibling to `SignConfig`) holding:

- `target_pi_sha: str` — defaults to Flask's running SHA on first creation.
- `flask_auto_update: bool` — defaults to `True` (matches today's behavior).
- `pi_auto_update: bool` — defaults to `True` (matches today's behavior).

Persisted in the existing SQLite DB alongside `SignConfig`, accessed via `MessageManager` (or a new lightweight adapter — see D5).

- **Why:** Settings survive Flask dyno restarts and re-deploys. SQLite is already the source of truth for `SignConfig`, so reusing it is consistent.
- **Alternative considered:** S3 (the operator's actual source of truth per the repo architecture). Rejected for v1 — adds latency to a setting that the operator toggles interactively and that the loader reads on boot. SQLite is fine for now; if settings need to survive DB loss, we promote later.
- **Alternative considered:** In-memory only, lost on Flask restart. Rejected — auto-upgrade intent needs to survive a Flask deploy (the deploy publishes a new target; the operator wants that target to persist).

### D3. Two auto-update flags (Flask-side and Pi-side), one combined decision

The actual upgrade-decision logic on the loader becomes:

```
def should_attempt_upgrade(active_sha, target_sha, pi_flag, flask_flag):
    if active_sha == target_sha:
        return False  # nothing to do
    if not flask_flag:
        return False  # Flask-level override: hold current
    if not pi_flag:
        return False  # Pi-level override: hold current
    return True
```

`force-upgrade` bypasses both flags (operator's explicit intent). Restart and shutdown don't read the flags; they just execute.

- **Why:** Lets the operator override auto-upgrade from either side. The Flask-side flag is what the operator toggles on the Settings page; the Pi-side flag is what the Pi reports back to Flask via the status flow (so the Settings page can show "Pi is currently refusing auto-updates" without forcing the Pi to apply that flag locally — it's UI state, not a hard brake).
- **Alternative considered:** Single flag. Rejected — the issue text explicitly calls for per-side control so the Pi can hold a version even when Flask wants to push a new one.
- **Alternative considered:** Treating both flags as hard brakes persisted on the Pi. Rejected for v1 because it requires the Pi to write a config file from a process that runs as root and may be killed mid-write; the report-back-via-status pattern is good enough for the operator's UI and avoids filesystem corruption risk.

### D4. Target Pi version defaults to Flask version; "Automatically update" greys it out

When the operator enables Flask-side "Automatically update", the Target Pi version input is disabled and tracks Flask version 1:1 via a JavaScript handler. Disabling the checkbox re-enables the input for manual entry.

- **Why:** Matches the issue text: "enabling it sets to Flask version and greys it out". Saves the operator from typing SHA strings in the common case.
- **Alternative considered:** Always-editable target with a separate "follow Flask" switch. Rejected — the issue specifies the greying behavior.

### D5. Settings changes publish a `type=command` envelope with `action=set-upgrade-settings`

When the operator saves changes on the Settings page, Flask:

1. Persists the new `PiUpgradeSettings` row in SQLite.
2. Publishes a `{"type":"command","payload":{"action":"set-upgrade-settings","target_pi_sha":"...","flask_auto_update":true,"pi_auto_update":true}}` envelope on `MQTT_TOPIC`.

The Pi (already subscribed) reads via a new `set_upgrade_settings` handler in `command_handlers.py`, which stores the latest payload in memory (e.g. `PiUpgradeState.from_env_or_settings()` helper) and triggers `check_for_update` semantics if the target SHA differs from `LINDSAY50_ACTIVE_SHA`.

- **Why:** Lets the Pi notice a settings change mid-flight instead of waiting for the next boot or next `check-for-update` MQTT hint. The envelope is one-shot (Flask publishes on Save click), not periodic — keeps the broker load flat.
- **Alternative considered:** Polling on a timer. Rejected — adds MQTT chatter and is no more reliable than the Settings page just publishing on save.
- **Alternative considered:** REST endpoint the Pi polls. Rejected — adds HTTP server surface to a Pi that may be on a flaky network; MQTT already works.

### D6. Pi auto-update flag is Pi-side state, reported back, but never written from the Settings page

The Settings page CANNOT toggle the Pi-side "Automatically update" checkbox — it's read-only, populated from the latest `StatusSnapshot`'s `pi_auto_update` field. The Pi toggles it locally by writing it to its status payload based on… nothing in v1. (See Open Questions.)

- **Why:** Avoids the operator writing a flag to a remote device without confirmation; a malicious or buggy operator-side action would be unrecoverable without SSH. Today the Pi always reports `pi_auto_update=True`; that field is reserved for future Pi-side logic (e.g. "auto-detect display regression and hold") and is read-only on the Flask UI.
- **Alternative considered:** Flask writes the Pi-side flag as a `set-upgrade-settings` payload. Deferred — the Pi-side flag isn't authoritative in v1, so writing it would give false confidence.
- **Alternative considered:** Drop the Pi-side flag entirely for v1. Rejected — the issue text explicitly asks for it.

### D7. UI: existing Settings page, new "Pi Upgrade Control" section

Renders below the existing Settings page sections (Sign Name, Twilio, etc.) and above the Sign Health section (added by `add-sign-status-reports`). Reuses the existing Bootstrap 5 form layout. The variant for `/playful/settings` gets the same section with Tailwind styles.

Three read-only version displays use the same `data-sign-status-field="<name>"` attribute pattern as the Sign Health section, so `sign_status.js` and `pi_upgrade_settings.js` can co-populate the two sections from the same status-WS client.

- **Why:** Reuses the existing Settings page chrome — no new navigation, no new auth gate, no new templating pattern. The dual `data-*` attributes let both modules populate their fields without stepping on each other.

### D8. Flask-side status reporting: same topic, same 30s cadence, smaller shape

Flask starts a `threading.Timer`-driven publisher (mirror of the Pi-side `_status_publisher` from `add-sign-status-reports`) that publishes a small JSON dict on `MQTT_STATUS_TOPIC` every 30s:

```json
{
  "active_sha": "abc123",
  "started_at": "2026-07-07T10:00:00Z",
  "uptime_seconds": 3600,
  "flask_auto_update": true,
  "target_pi_sha": "abc123"
}
```

The browser's existing status-WS client (per `add-sign-status-reports`) routes payloads by source — `active_sha` + `started_at` come from the Pi, Flask's come from a `source` field. `sign_status.js` is updated to merge both into a single `lastStatus` map keyed by SHA + `lastFlaskStatus` for Flask-specific fields.

- **Why:** The operator wants to know what the *server* thinks it's running (Flask version) and what the *sign* thinks it's running (Pi version). Both fields are needed for "is everything in sync?". One topic, one cadence, one browser subscriber.
- **Alternative considered:** Separate topic `MQTT_FLASK_STATUS_TOPIC`. Deferred — adds a third WS client to the browser. One-topic is fine while payload sizes stay small (Flask status is ~200 bytes).

### D9. Force-upgrade, restart, shutdown are 202-on-publish with no client confirmation

The Flask endpoint publishes the command envelope and returns `202 Accepted` immediately. There is no per-client ACK, no request-to-confirm modal (besides a regular browser `confirm()` dialog before publishing). The operator sees success/failure indirectly: the status flow's next snapshot reflects the new state (Pi version changes for upgrade; Pi goes offline for restart/shutdown).

- **Why:** Implementing a request-reply RPC over MQTT adds a new envelope type and a correlation-ID roundtrip for low-value UX. The Settings page already shows Pi status via the Sign Health section — that's the operator's actual feedback channel.
- **Alternative considered:** Wait for Pi ACK before responding 200. Rejected — the loader doesn't ack, and adding ACKing couples the command flow to a status reply that may not come (Pi might be on a flaky network or already past the handler).

### D10. Loader fallthrough is unchanged: any failure → exec existing `current/.../main.py`

The three new handlers and any new loader logic preserve the `self-upgrading-matrix-controller` invariant: a failure anywhere in the upgrade path drops the user back into the currently-running `v-<sha>/main.py` via `os.execvpe`. No new bricks.

- **Why:** The original loader design was specifically built around "Pi can never brick itself", and adding operator-driven commands is the highest-risk surface for introducing a brick. The fallthrough is the existing safety net; this change must not weaken it.

## Risks / Trade-offs

- **Operator toggles Flask-side auto-update off, then forgets Flask is now stale.** → The Pi will hold the current version; the operator sees both Flask and Pi versions on the Settings page and can re-enable. No silent data loss; the visual mismatch is the cue.
- **Force-upgrade arrives while a `check-for-update` flow is already mid-stage.** → Both commands `os.execvpe` into the loader, so the second one preempts the first. The loader's SHA check is idempotent — a no-op on match. Worst case: the new version probes twice. No brick risk because the loader's fallthrough is intact.
- **Browser loses the status-WS connection while operator is editing target SHA.** → The Save button sends a POST to Flask; Flask persists the change and publishes the `set-upgrade-settings` envelope. The Pi does not depend on the browser's WS to receive the change. Only the auto-update-flag display in the UI lags the persistence.
- **Settings stored in SQLite are lost if SQLite is wiped** (e.g. S3 rebuild-from-S3 on startup per `sqlite.py`). → SQLite rebuilds from S3; `SignConfig` rebuilds from S3. `PiUpgradeSettings` needs the same rebuild path — added in tasks. Without it, a Flask-side SQLite wipe resets the target to Flask's current SHA (which is approximately safe but changes operator intent without notice).
- **Flask-side auto-update flag is off, but a settings save re-evaluates the existing target match.** → The operator toggles the flag, leaves the Target field at the current value. The next loader boot re-evaluates: target ≠ active → flag off → no upgrade. Match to operator intent.
- **Pi-side auto-update flag is a UI-only stub in v1.** → Operator sees a checkbox that's effectively always True. Documented in the Settings UI ("Reserved — currently always on"). Avoids the false-confidence risk described in D6.
- **`sudo reboot` in the restart handler fails silently** (sandbox misconfig, missing NOPASSWD). → The handler logs the `subprocess.run` result; if non-zero, the existing main.py keeps running and the operator sees the Pi never went offline via the status pill. UI surfaces a transient "Restart command sent — check Pi" hint; operator verifies via the existing dashboard pill.

## Migration Plan

1. **Code lands on `main`:** All Python + JS + template changes merged. Settings persist behind a new `settings_version = 1` discriminator so a deploy against an old DB doesn't crash on the missing columns.
2. **Flask deploy:** `git push heroku main`. Flask restarts → on startup, `PiUpgradeSettings` table is created if missing; default row inserted with `target_pi_sha = HEROKU_SLUG_COMMIT`, `flask_auto_update = True`, `pi_auto_update = True`. Existing `SignConfig` rows untouched.
3. **Pi receives nothing new at boot.** Auto-upgrade continues to work exactly as before on the existing code path; the new flags are not read until the operator toggles them. **No Pi-side action required for migration** — operators can keep using the system as-is while we land the change.
4. **Operator visits the Settings page** → sees the new Pi Upgrade Control section. All three version fields populate within the first status snapshot (≤30s after the operator opens the page; usually immediately because the Pi has been publishing all along).
5. **Operator toggles a flag or changes Target.** Saved via POST → SQLite row updated → MQTT `set-upgrade-settings` envelope published → Pi's new `set_upgrade_settings` handler reads the latest target + flags → if upgrade-conditions met AND target ≠ active, `check_for_update` semantics trigger.
6. **Operator issues Force Upgrade / Restart / Shutdown.** Click → `confirm()` dialog → POST → 202 → handler executes.
7. **Rollback (if anything goes wrong):** `heroku rollback v<previous>` brings Flask back to the pre-change version. The pre-change `self-upgrading-matrix-controller` design does not consult `target_pi_sha` for upgrade decisions, so rollback restores the original "always upgrade on Flask SHA change" behavior — the Pi may upgrade one extra time during the rollback, but cannot brick.

## Open Questions

- **Pi-side auto-update flag: when does the Pi ever set it to False?** v1 ships the field as a UI stub (always True). A future change might add Pi-side logic (e.g. "if render pattern X throws for >5 minutes, set auto-update off and notify Flask"). Out of scope here; flagged for follow-up.
- **GC of old upgrade-settings envelopes.** The Pi's `set_upgrade_settings` handler stores the latest target + flags in memory and forgets older payloads; no disk state. If Flask publishes 1k envelopes while the Pi is restarting, the post-restart Pi loads the latest from SQLite-rebuild (D2) and overwrites. No accumulation.
- **Confirm-modals for destructive commands.** Restart and Shutdown get `confirm()` dialogs in v1; we may want a typed-confirmation pattern (force-upgrade is non-destructive; restart/shutdown are). Tracked in tasks.
